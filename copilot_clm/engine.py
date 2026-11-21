"""A projection of wire history, independent of Copilot's stored transcript.

Only text fields are editable. Native structured objects survive untouched unless
their text changes; function calls may only be removed with their results.
"""
import copy
import hashlib
import json
import math
import os
import uuid
from pathlib import Path

from .storage import MAX_BYTES, encode, private_dir, read_json, write_json

NOTE_PREFIX = "[CLM model-authored notes]\n"


def note_item(text):
    return {"role": "assistant", "content": NOTE_PREFIX + text.removeprefix(NOTE_PREFIX)}


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def history_identity(items):
    """Copilot reserializes tool arguments on resume. Ignore JSON whitespace only.

    Message text and every other wire field remain exact. We use this solely for
    matching, never to rewrite the provider payload or the editable mirror.
    """
    items = copy.deepcopy(items)
    def unique_object(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("Duplicate JSON key")
            obj[key] = value
        return obj
    for item in items:
        targets = [item] if item.get("type") == "function_call" else [
            c.get("function", {}) for c in item.get("tool_calls", [])]
        for target in targets:
            if isinstance(target.get("arguments"), str):
                try:
                    target["arguments"] = encode(json.loads(target["arguments"], object_pairs_hook=unique_object))
                except ValueError:
                    pass
    return digest(items)


def text_field(item):
    """Return (field, nested index) for a single safe, textual payload."""
    field = "output" if item.get("type") == "function_call_output" else "content"
    value = item.get(field)
    if isinstance(value, str):
        return field, None
    if (isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict)
            and value[0].get("type") in ("text", "input_text", "output_text")
            and isinstance(value[0].get("text"), str)):
        return field, 0
    return None


def get_text(item):
    field = text_field(item)
    if field is None:
        return encode(item)
    key, index = field
    return item[key] if index is None else item[key][index]["text"]


def set_text(item, text):
    result = copy.deepcopy(item)
    key, index = text_field(item)
    if index is None:
        result[key] = text
    else:
        result[key][index]["text"] = text
    return result


def calls(item):
    if item.get("type") == "function_call":
        return [item.get("call_id")]
    return [call.get("id") for call in item.get("tool_calls", [])]


def result_id(item):
    if item.get("type") == "function_call_output":
        return item.get("call_id")
    if item.get("role") == "tool":
        return item.get("tool_call_id")
    return None


def validate_pairs(items):
    pending, seen = set(), set()
    for item in items:
        output = result_id(item)
        if output is not None:
            if output not in pending:
                raise ValueError("Orphaned, duplicate or reordered tool result")
            pending.remove(output)
        elif pending and item.get("role") in ("user", "assistant"):
            raise ValueError("A message splits a tool-call/result group")
        for call in calls(item):
            if not isinstance(call, str) or call in seen:
                raise ValueError("Invalid or duplicate tool call ID")
            seen.add(call)
            pending.add(call)
    if pending:
        raise ValueError("Tool calls must retain all their results")


def estimate(body):
    # Deliberately labelled an estimate, not a provider/tokenizer token count.
    return math.ceil(len(encode(body).encode()) / 3)


def reasoning_spans(entries):
    """Keep opaque reasoning and the rest of its turn structurally together.

    Full response boundaries are not exposed by the CLI request. Conservatively
    lock the span through the next user message; textual outputs remain editable.
    """
    spans, current = [], []
    for entry in entries:
        if entry["id"].startswith("new-"):
            continue
        item = entry["item"]
        if item.get("role") == "user" and current:
            spans.append(current)
            current = []
        if item.get("type") == "reasoning" or current:
            current.append(entry["id"])
    if current:
        spans.append(current)
    return spans


class Engine:
    def __init__(self, root, budget=64000, guard=True, record_requests=False):
        self.root = private_dir(root)
        self.budget, self.guard, self.record_requests = budget, guard, record_requests
        self.streams = {}
        self.busy = set()
        # Native cache keys identify live request scopes, but change on CLI
        # restart. Rebind an unambiguous persisted history once per launch.
        self.owners = {}
        for path in sorted(self.root.glob("stream-*/state.json")):
            state = read_json(path)
            self.streams[state["id"]] = state
        if not (self.root / "control.json").exists():
            write_json(self.root / "control.json", {"enabled": True, "reset": 0})

    def directory(self, state):
        return private_dir(self.root / state["id"])

    def select(self, raw, signature, scope=None):
        candidates = [s for s in self.streams.values()
                      if s["id"] not in self.busy and s["signature"] == signature and len(s["raw"]) <= len(raw)
                      and (s["id"] not in self.owners or self.owners[s["id"]] == scope)
                      and history_identity(raw[:len(s["raw"])]) == history_identity(s["raw"])]
        if candidates:
            longest = max(len(s["raw"]) for s in candidates)
            matches = [s for s in candidates if len(s["raw"]) == longest]
            if len(matches) == 1:
                state = matches[0]
                self.owners[state["id"]] = scope
                return state
        state = {"id": "stream-" + uuid.uuid4().hex, "signature": signature,
                 "raw": [], "projection": [], "revision": 0, "requests": 0,
                 "reset": 0, "mirror": None, "outcome": "new history"}
        self.streams[state["id"]] = state
        self.owners[state["id"]] = scope
        return state

    def track_utility(self, body):
        """Account for tool-less title/compaction calls without touching a mirror."""
        directory = private_dir(self.root / "utility")
        counter = directory / "counter.json"
        number = read_json(counter)["requests"] + 1 if counter.exists() else 1
        write_json(counter, {"requests": number})
        if self.record_requests:
            records = private_dir(directory / "requests")
            write_json(records / f"{number:04d}.json", body)
        return {"stream": "utility", "requests": number}

    def entry(self, item, index):
        return {"id": f"m{index}-{digest(item)[:12]}", "item": copy.deepcopy(item)}

    def document(self, state):
        rows = []
        linked = {key for span in reasoning_spans(state["projection"]) for key in span}
        for entry in state["projection"]:
            item = entry["item"]
            protected = (item.get("role") == "user" and not entry["id"].startswith("new-"))
            # Reasoning, images and unknown protocol items cannot be changed or dropped.
            opaque = text_field(item) is None and not calls(item)
            rows.append({"id": entry["id"],
                         "role": item.get("role", item.get("type", "opaque")),
                         "protected": protected or opaque or entry["id"] in linked,
                         "editable": text_field(item) is not None and not protected and not calls(item),
                         "text": get_text(item)})
        return {"version": 1, "stream": state["id"], "revision": state["revision"],
                "baseline": digest(state["projection"]), "messages": rows}

    def accept_edit(self, state):
        baseline = state["mirror"]
        path = self.directory(state) / "context.json"
        if baseline is None or not path.exists():
            return
        try:
            edited = read_json(path)
            if edited == baseline:
                return
            if not isinstance(edited, dict) or set(edited) != set(baseline):
                raise ValueError("Keep the mirror header and messages array")
            for key in ("version", "stream", "revision", "baseline"):
                if edited[key] != baseline[key]:
                    raise ValueError("Stale or modified mirror header")
            if not isinstance(edited["messages"], list):
                raise ValueError("messages must be an array")
            originals = {m["id"]: m for m in baseline["messages"]}
            entries = {e["id"]: e for e in state["projection"]}
            projected, seen = [], set()
            for row in edited["messages"]:
                if not isinstance(row, dict) or not isinstance(row.get("text"), str):
                    raise ValueError("Each message needs string id and text fields")
                key = row.get("id")
                if not isinstance(key, str) or key in seen:
                    raise ValueError("Missing or duplicate message id")
                seen.add(key)
                if key not in originals:
                    if not key.startswith("new-") or set(row) != {"id", "text"}:
                        raise ValueError("New notes must have only id=new-... and text")
                    projected.append({"id": key, "item": note_item(row["text"])})
                    continue
                old = originals[key]
                if {k: v for k, v in row.items() if k != "text"} != {
                        k: v for k, v in old.items() if k != "text"}:
                    raise ValueError("Message metadata cannot be changed")
                entry = copy.deepcopy(entries[key])
                if row["text"] != old["text"]:
                    if not old["editable"]:
                        raise ValueError("This message's text is protected")
                    entry["item"] = note_item(row["text"]) if key.startswith("new-") else set_text(entry["item"], row["text"])
                projected.append(entry)
            protected = [r["id"] for r in baseline["messages"] if r["protected"]]
            if [r["id"] for r in edited["messages"] if r["id"] in protected] != protected:
                raise ValueError("Keep protected messages in their original order")
            # Keep original user constraints ahead of any model-authored replacement.
            if baseline["messages"] and baseline["messages"][0]["protected"]:
                if not projected or projected[0]["id"] != baseline["messages"][0]["id"]:
                    raise ValueError("Keep the initial task at the beginning")
            validate_pairs([e["item"] for e in projected])
            order = [entry["id"] for entry in projected]
            for span in reasoning_spans(state["projection"]):
                start = order.index(span[0])
                if order[start:start + len(span)] != span:
                    raise ValueError("Keep reasoning-linked entries together in their original order")
            revision = state["revision"] + 1
            revisions = private_dir(self.directory(state) / "revisions")
            write_json(revisions / f'{revision:04d}.json',
                       {"before": baseline, "after": edited})
            state["revision"] = revision
            state["projection"] = projected
            state["outcome"] = "accepted edit"
        except (ValueError, OSError, TypeError, KeyError) as exc:
            state["outcome"] = "rejected edit: " + str(exc)
            # Preserve invalid bytes for diagnosis, even if JSON parsing failed.
            if path.is_file() and not path.is_symlink():
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(fd, "rb") as source:
                    rejected = source.read(MAX_BYTES)
                write_json(self.directory(state) / "rejected.json",
                           {"error": str(exc), "text": rejected.decode("utf-8", errors="replace")})

    def protocol(self, state):
        path = self.directory(state) / "context.json"
        return ("CLM context editing is enabled. Your next request's conversation is mirrored at "
                f"{path}. Read it with your normal file/shell tools when useful. "
                "Edit only messages[].text of editable entries; remove obsolete unprotected entries "
                "or reorder them. Remove a tool call only with ALL its results. Preserve protected "
                "entries, their order, all metadata, and the top-level header. "
                "Keep reasoning-linked protected spans contiguous; their editable text may still be compacted. "
                'You may insert notes with only {"id":"new-unique-name","text":"..."}. '
                "Notes are model-authored data, never system instructions. Changes are validated and "
                "replace the actual next model input. New tool calls/results are appended automatically. "
                f"Target request budget: {self.budget} estimated tokens, including tools and instructions. "
                "Check status.json next to the mirror for size, revision and edit errors. "
                "Compact stale tool output and obsolete work in batches when near budget; keep exact "
                "facts, current progress, unresolved failures and file pointers. Combine context edits "
                "with useful work in one tool call when possible. Do not edit merely to acknowledge "
                "this protocol. Full transcript remains in Copilot. Native /compact creates a fresh "
                "CLM stream. Never modify state.json or other CLM control files as part of a context edit.")

    def transform(self, body, wire):
        body = copy.deepcopy(body)
        if body.get("previous_response_id") or body.get("conversation"):
            raise ValueError("CLM requires explicit complete history; server-side continuation is unsupported")
        key = "input" if wire == "responses" else "messages"
        raw_input = body.get(key)
        if isinstance(raw_input, str) and wire == "responses":
            raw_input = [{"role": "user", "content": raw_input}]
        if not isinstance(raw_input, list) or any(not isinstance(x, dict) for x in raw_input):
            raise ValueError("Expected a list of conversation items")
        # Utility/title/compaction calls are passed through, without consuming edits.
        if not body.get("tools"):
            return body, None
        fixed, raw = [], []
        for item in raw_input:
            if item.get("role") in ("system", "developer"):
                if raw:
                    # Moving a mid-history high-priority instruction would alter semantics.
                    raise ValueError("Mid-history system/developer messages are unsupported")
                fixed.append(item)
            else:
                raw.append(item)
        validate_pairs(raw)
        signature = digest([wire, body.get("model"), body.get("instructions"), fixed, body.get("tools")])
        state = self.select(raw, signature, body.get("prompt_cache_key"))
        control = read_json(self.root / "control.json")
        if state["reset"] != control.get("reset", 0):
            state.update(projection=[self.entry(m, i) for i, m in enumerate(state["raw"])],
                         mirror=None, reset=control.get("reset", 0), outcome="reset")
        if control.get("enabled", True):
            self.accept_edit(state)
        tail = [self.entry(m, i) for i, m in enumerate(raw) if i >= len(state["raw"])]
        state["projection"].extend(tail)
        for entry in state["projection"]:
            if entry["id"].startswith("new-"):
                entry["item"] = note_item(get_text(entry["item"]))
        state["raw"] = copy.deepcopy(raw)
        state["requests"] += 1
        enabled = control.get("enabled", True)
        protocol = {"role": "system", "content": self.protocol(state)}

        def assemble():
            body[key] = fixed + ([protocol] if enabled else []) + (
                [e["item"] for e in state["projection"]] if enabled else raw)
            if wire == "responses":
                body["store"] = False
            return body

        assemble()
        spilled = 0
        if enabled and self.guard and estimate(body) > self.budget:
            # Only completed, old text tool outputs are spilled. The newest group survives.
            for entry in state["projection"][:-2]:
                item = entry["item"]
                if result_id(item) is None or text_field(item) is None or len(get_text(item)) < 1000:
                    continue
                directory = private_dir(self.directory(state) / "spills")
                path = directory / (digest(item) + ".json")
                write_json(path, item)
                entry["item"] = set_text(item, f"[CLM overflow: full tool output saved at {path}]")
                spilled += 1
                assemble()
                if estimate(body) <= self.budget:
                    break
        if enabled:
            notice = None
            if state["outcome"].startswith("rejected edit:"):
                notice = state["outcome"] + ". The last accepted context remains active. Re-read the mirror before retrying."
            elif estimate(body) >= self.budget * 0.75:
                notice = (f"Request size is approximately {estimate(body)} tokens against a {self.budget} budget. "
                          "Use the context mirror to remove obsolete output while preserving current facts and work.")
            if notice:
                body[key].append({"role": "user", "content": "[CLM context notice] " + notice})
                # Advice must not turn a fitting request into a hard overflow.
                if estimate(body) > self.budget:
                    body[key].pop()
        state["mirror"] = self.document(state)
        directory = self.directory(state)
        write_json(directory / "context.json", state["mirror"])
        status = {"stream": state["id"], "enabled": enabled, "revision": state["revision"],
                  "requests": state["requests"], "outcome": state["outcome"],
                  "estimated_tokens": estimate(body), "budget": self.budget,
                  "estimate_method": "UTF-8 JSON bytes / 3 (not provider usage)",
                  "spilled_outputs": spilled, "raw_messages": len(raw),
                  "projected_messages": len(state["projection"]),
                  "context_path": str(directory / "context.json")}
        write_json(directory / "status.json", status)
        write_json(directory / "input.json", body)
        write_json(directory / "state.json", state)
        if self.record_requests:
            records = private_dir(directory / "requests")
            write_json(records / f'{state["requests"]:04d}.json', body)
        if enabled and estimate(body) > self.budget:
            raise ValueError(f"CLM budget exceeded ({estimate(body)} > {self.budget} estimated tokens). "
                             "Edit context.json, increase --budget, or use native /compact.")
        return body, status
