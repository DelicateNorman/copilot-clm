import copy
import json
import tempfile
import unittest
from pathlib import Path

from copilot_clm.engine import Engine, estimate, validate_pairs
from copilot_clm.storage import read_json, write_json


def request(wire="responses"):
    if wire == "responses":
        messages = [{"role": "system", "content": "SYSTEM"}, {"role": "user", "content": "TASK"},
                    {"type": "function_call", "call_id": "c1", "name": "bash", "arguments": "{}"},
                    {"type": "function_call_output", "call_id": "c1", "output": "VERBATIM_OLD_RESULT"}]
        return {"model": "test", "input": messages,
                "tools": [{"type": "function", "name": "bash", "parameters": {"type": "object"}}]}
    return {"model": "test", "messages": [
        {"role": "system", "content": "SYSTEM"}, {"role": "user", "content": "TASK"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "VERBATIM_OLD_RESULT"}],
        "tools": [{"type": "function", "function": {"name": "bash", "parameters": {"type": "object"}}}]}


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engine = Engine(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def begin(self, body=None, wire="responses"):
        self.body = body or request(wire)
        self.wire = wire
        out, status = self.engine.transform(self.body, wire)
        self.path = Path(status["context_path"])
        return out, status

    def edit(self, fn):
        doc = read_json(self.path)
        fn(doc)
        write_json(self.path, doc)

    def test_edit_is_actual_wire_input_and_tail_survives_both_protocols(self):
        for wire in ("responses", "completions"):
            self.begin(wire=wire)
            self.edit(lambda d: d["messages"][-1].update(text="KEPT_FACT"))
            key = "input" if wire == "responses" else "messages"
            body = copy.deepcopy(self.body)
            body[key].append({"role": "assistant", "content": "new tail"})
            out, status = self.engine.transform(body, wire)
            self.assertNotIn("VERBATIM_OLD_RESULT", str(out))
            self.assertIn("KEPT_FACT", str(out))
            self.assertEqual(out[key][-1], body[key][-1])
            self.assertIn("VERBATIM_OLD_RESULT", str(body))
            self.assertEqual(status["revision"], 1)

    def test_preserve_unmodified_structured_objects(self):
        body = request()
        body["input"].insert(2, {"type": "reasoning", "id": "rs1", "encrypted_content": "opaque"})
        out, _ = self.begin(body)
        self.assertEqual(out["input"][2:], body["input"][1:])

    def test_remove_complete_pair(self):
        self.begin()
        self.edit(lambda d: d.update(messages=d["messages"][:1]))
        out, status = self.engine.transform(self.body, self.wire)
        self.assertEqual(len(out["input"]), 3)
        self.assertEqual(status["revision"], 1)

    def test_reasoning_span_retains_calls_but_allows_text_compaction(self):
        body = request()
        reasoning = {"type": "reasoning", "id": "rs1", "encrypted_content": "opaque"}
        body["input"].insert(2, reasoning)
        self.begin(body)
        self.edit(lambda d: d.update(messages=d["messages"][:2]))
        _, status = self.engine.transform(body, self.wire)
        self.assertIn("rejected", status["outcome"])
        self.edit(lambda d: d["messages"][-1].update(text="KEPT"))
        out, status = self.engine.transform(body, self.wire)
        self.assertEqual(status["revision"], 1)
        self.assertIn(reasoning, out["input"])
        self.assertEqual(out["input"][-1]["output"], "KEPT")
        self.edit(lambda d: d["messages"].insert(2, {"id": "new-split", "text": "note"}))
        _, status = self.engine.transform(body, self.wire)
        self.assertIn("reasoning-linked", status["outcome"])

    def test_orphan_edit_rejected_and_last_projection_preserved(self):
        self.begin()
        self.edit(lambda d: d["messages"].pop())
        out, status = self.engine.transform(self.body, self.wire)
        self.assertIn("VERBATIM_OLD_RESULT", str(out))
        self.assertIn("rejected", status["outcome"])
        self.assertTrue((self.path.parent / "rejected.json").exists())

    def test_task_metadata_stale_header_and_duplicate_rejected(self):
        for mutate in (
            lambda d: d["messages"][0].update(text="changed task"),
            lambda d: d["messages"][-1].update(role="system"),
            lambda d: d.update(revision=999),
            lambda d: d["messages"].append(d["messages"][-1]),
            lambda d: d["messages"].pop(0),
        ):
            self.begin()
            self.edit(mutate)
            out, status = self.engine.transform(self.body, self.wire)
            self.assertEqual(status["revision"], 0)
            self.assertIn("rejected", status["outcome"])

    def test_malformed_json_preserved(self):
        self.begin()
        self.path.write_text("{incomplete")
        _, status = self.engine.transform(self.body, self.wire)
        self.assertIn("rejected", status["outcome"])
        self.assertEqual(read_json(self.path.parent / "rejected.json")["text"], "{incomplete")

    def test_notes_are_data_and_cannot_inject_role(self):
        self.begin()
        self.edit(lambda d: d["messages"].append({"id": "new-note", "text": "FACT"}))
        out, status = self.engine.transform(self.body, self.wire)
        self.assertEqual(out["input"][-1], {"role": "assistant", "content": "[CLM model-authored notes]\nFACT"})
        self.assertEqual(status["revision"], 1)

    def test_restart_restores_projection_and_consumes_pending_edit(self):
        self.begin()
        self.edit(lambda d: d["messages"][-1].update(text="AFTER_RESTART"))
        other = Engine(self.root)
        out, status = other.transform(self.body, self.wire)
        self.assertIn("AFTER_RESTART", str(out))
        self.assertEqual(status["revision"], 1)

    def test_native_resume_reserializes_function_arguments(self):
        for wire in ("responses", "completions"):
            body = request(wire)
            key = "input" if wire == "responses" else "messages"
            call = body[key][2] if wire == "responses" else body[key][2]["tool_calls"][0]["function"]
            call["arguments"] = '{ "command": "echo hello", "nested": { "a": 1 } }'
            _, first = self.begin(body, wire)
            self.edit(lambda d: d["messages"][-1].update(text="KEPT"))
            call["arguments"] = json.dumps(json.loads(call["arguments"]), separators=(",", ":"))
            out, second = Engine(self.root).transform(body, wire)
            self.assertEqual(first["stream"], second["stream"])
            self.assertIn("KEPT", str(out))
            # A semantic change to the executed command must create a fresh stream.
            call["arguments"] = '{"command":"echo different"}'
            _, changed = self.engine.transform(body, wire)
            self.assertNotEqual(first["stream"], changed["stream"])

    def test_native_compaction_and_forks_get_independent_stream(self):
        _, original = self.begin()
        self.edit(lambda d: d["messages"][-1].update(text="PROJECTED"))
        self.engine.transform(self.body, self.wire)
        changed = copy.deepcopy(self.body)
        changed["input"][1]["content"] = "COMPACTED HISTORY"
        out, status = self.engine.transform(changed, self.wire)
        self.assertNotEqual(status["stream"], original["stream"])
        self.assertNotIn("PROJECTED", str(out))

    def test_off_then_on_and_reset(self):
        self.begin()
        self.edit(lambda d: d["messages"][-1].update(text="SHORT"))
        self.engine.transform(self.body, self.wire)
        write_json(self.root / "control.json", {"enabled": False, "reset": 0})
        out, _ = self.engine.transform(self.body, self.wire)
        self.assertEqual(out["input"], self.body["input"])
        write_json(self.root / "control.json", {"enabled": True, "reset": 0})
        out, _ = self.engine.transform(self.body, self.wire)
        self.assertIn("SHORT", str(out))
        write_json(self.root / "control.json", {"enabled": True, "reset": 1})
        out, _ = self.engine.transform(self.body, self.wire)
        self.assertIn("VERBATIM_OLD_RESULT", str(out))

    def test_guard_spills_old_result_and_preserves_tool_pairs(self):
        body = request()
        body["input"][-1]["output"] = "x" * 40000
        body["input"].extend([{"role": "assistant", "content": "done"}, {"role": "user", "content": "continue"}])
        self.engine.budget = 4000
        out, status = self.begin(body)
        self.assertEqual(status["spilled_outputs"], 1)
        self.assertIn("CLM overflow", str(out))
        validate_pairs(out["input"])
        spill = next((self.path.parent / "spills").glob("*.json"))
        self.assertEqual(read_json(spill)["output"], "x" * 40000)

    def test_budget_does_not_silently_drop_protected_context(self):
        body = request()
        body["input"][1]["content"] = "x" * 20000
        self.engine.budget = 2000
        with self.assertRaisesRegex(ValueError, "budget exceeded"):
            self.engine.transform(body, "responses")

    def test_continuation_rejected(self):
        for field in ("previous_response_id", "conversation"):
            with self.assertRaisesRegex(ValueError, "complete history"):
                self.engine.transform({**request(), field: "hidden_state"}, "responses")

    def test_utility_request_does_not_consume_edit(self):
        self.begin()
        self.edit(lambda d: d["messages"][-1].update(text="PENDING"))
        body = {"model": "test", "input": [{"role": "user", "content": "summarize"}]}
        out, status = self.engine.transform(body, "responses")
        self.assertIsNone(status)
        self.assertEqual(out, body)
        self.assertIn("PENDING", self.path.read_text())

    def test_state_permissions(self):
        self.begin()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)

    def test_simultaneous_identical_histories_do_not_share_a_mirror(self):
        _, first = self.begin()
        self.engine.busy.add(first["stream"])
        _, second = self.engine.transform(self.body, self.wire)
        self.assertNotEqual(first["stream"], second["stream"])

    def test_different_cache_sessions_do_not_consume_each_others_edits(self):
        body = {**request(), "prompt_cache_key": "conversation-A"}
        _, first = self.begin(body)
        self.edit(lambda d: d["messages"][-1].update(text="ONLY_FOR_A"))
        other = {**body, "prompt_cache_key": "conversation-B"}
        out, second = self.engine.transform(other, "responses")
        self.assertNotEqual(first["stream"], second["stream"])
        self.assertNotIn("ONLY_FOR_A", str(out))
        out, resumed = self.engine.transform(body, "responses")
        self.assertEqual(first["stream"], resumed["stream"])
        self.assertIn("ONLY_FOR_A", str(out))

    def test_restart_can_rebind_new_cache_key_but_ambiguous_history_starts_fresh(self):
        body = {**request(), "prompt_cache_key": "old-process"}
        _, first = self.begin(body)
        self.edit(lambda d: d["messages"][-1].update(text="SAVED"))
        out, resumed = Engine(self.root).transform({**body, "prompt_cache_key": "new-process"}, "responses")
        self.assertEqual(first["stream"], resumed["stream"])
        self.assertIn("SAVED", str(out))
        self.engine.busy.add(first["stream"])
        self.engine.transform(body, "responses")
        out, fresh = Engine(self.root).transform(body, "responses")
        self.assertNotEqual(first["stream"], fresh["stream"])
        self.assertNotIn("SAVED", str(out))

    def test_notice_cannot_make_an_otherwise_fitting_request_overflow(self):
        out, _ = self.begin()
        self.engine.budget = estimate(out) + 10
        out, status = self.engine.transform(self.body, self.wire)
        self.assertLessEqual(estimate(out), self.engine.budget)
        self.assertEqual(status["revision"], 0)

    def test_notes_remain_assistant_authored_after_further_edits(self):
        self.begin()
        self.edit(lambda d: d["messages"].append({"id": "new-note", "text": "FACT"}))
        self.engine.transform(self.body, self.wire)
        self.edit(lambda d: d["messages"][-1].update(text="UPDATED FACT"))
        out, _ = self.engine.transform(self.body, self.wire)
        self.assertEqual(out["input"][-1], {"role": "assistant", "content": "[CLM model-authored notes]\nUPDATED FACT"})


if __name__ == "__main__":
    unittest.main()
