# copilot-clm

Editable context for the **native GitHub Copilot CLI**. The model sees a file
containing its working conversation, edits that file with Copilot's normal
tools, and the validated projection becomes its next actual provider request.
Copilot keeps its own terminal UI, tool execution, permissions and transcript.

This is an independent, experimental community adapter inspired by
[pi-clm](https://github.com/lolipopshock/pi-clm) and
[opencode-clm](https://github.com/bcmyguest/opencode-clm). It implements the
context-as-file mechanism from [Context Language Models](https://github.com/facebookresearch/context-language-models).
It does not include the research harness, trained weights or suffix cache reuse.

## Install and run

Requires Python 3.10+, Linux/macOS, and a separately installed Copilot CLI.
Tested with **Copilot CLI 1.0.114**. No Python runtime dependencies.
The package is not yet published to PyPI/npm or a plugin marketplace.

Install from GitHub:

```bash
uv tool install git+https://github.com/DelicateNorman/copilot-clm.git
copilot-clm run --upstream http://127.0.0.1:4141/v1 --model YOUR_MODEL
```

Alternatively, clone this repository and install from its root:

```bash
pipx install .
# Or run directly without installation:
python3 -m copilot_clm run --upstream http://127.0.0.1:4141/v1 --model gpt-6-astra
```

Supply an existing OpenAI-compatible provider or gateway. The default upstream is
`CLM_UPSTREAM_URL`, `COPILOT_BASE_URL`, `COPILOT_PROVIDER_BASE_URL`, then
`http://127.0.0.1:4141/v1`, in that order. This adapter does not start or modify a gateway.
An API key, when needed, comes from `CLM_API_KEY`, `COPILOT_API_KEY`, or
`COPILOT_PROVIDER_API_KEY`. Keys are never written to adapter state.
For a remote provider, set `CLM_UPSTREAM_URL` to its API base URL and
`CLM_API_KEY` to your key, then run `copilot-clm run --model YOUR_MODEL`.
This release uses static Bearer authentication; key commands, custom headers and
Azure/Anthropic/Google native protocols are not implemented. Use an OpenAI-compatible
gateway for those providers. Responses is the default; use `--wire-api completions`
for Chat Completions providers. The model must support streaming and tool calls.

Pass native Copilot options after `--`:

```bash
copilot-clm run --model gpt-6-astra --budget 64k -- -p 'Fix the failing tests'
copilot-clm run --model my-model --wire-api completions -- --reasoning-effort low
```

The launcher does not add auto-approval flags. Copilot's usual permissions apply.
It grants visibility of its private state directory with `--add-dir`, so the agent
can read and edit the mirror. Provider settings are scoped to the child process;
the user's saved Copilot configuration and CLI binary are not modified.

## Why a launcher?

Pi and OpenCode expose whole-context transformation hooks. Copilot's documented
[hooks](https://docs.github.com/en/copilot/reference/hooks-reference) do not expose
that operation; `preCompact` is a notification. A skill or MCP server alone cannot
replace the history Copilot sends to its model.

This adapter uses Copilot's supported
[BYOK provider interface](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/use-byok-models):

```text
native Copilot CLI -> private local CLM endpoint -> your configured provider
                             |
                        context.json
                             ^
                    Copilot's file/shell tools
```

The endpoint binds only to `127.0.0.1`, requires a random per-launch token, and
forwards only supported inference routes. It streams upstream responses unchanged.
It neither intercepts GitHub login credentials nor routes GitHub's built-in model
service automatically. An existing provider endpoint is required. Accordingly,
`copilot plugin install copilot-clm` is **not** an installation command for this release.

## Editing and controls

The system protocol tells the model where its `context.json` is. Each request
publishes a versioned JSON mirror, with stable message IDs and editable text:

```json
{
  "version": 1,
  "stream": "stream-...",
  "revision": 0,
  "baseline": "...",
  "messages": [
    {"id": "m0-...", "role": "user", "protected": true, "editable": false, "text": "The task"},
    {"id": "m1-...", "role": "assistant", "protected": false, "editable": true, "text": "Old progress"}
  ]
}
```

The model can change editable text, delete obsolete unprotected messages, reorder
them, and add `{ "id": "new-unique-name", "text": "facts or progress" }` notes.
All original user messages are protected; system/developer instructions are outside
the mirror. Tool calls are immutable and can only be deleted with their results.
Unknown/opaque items, images and reasoning objects are preserved. Edited text
retains its original role and call IDs; new notes use the **assistant** role and a
persistent model-authored label. Untouched wire objects are preserved without
flattening structured content. A reasoning item and the remaining original items
of its turn must stay together; editable text in that span can still be shortened.
This conservative restriction protects providers' reasoning/tool dependencies.

Validation rejects stale headers, duplicate IDs, role/metadata changes, removed
protected entries and broken tool-call/result groups. A rejected edit keeps the
last accepted projection and saves `rejected.json`; the next request gets a short
error notice. New history since the mirror was written is appended automatically.

From a second terminal, use the state path printed at launch:

```bash
copilot-clm status --state-dir /path/from/banner
copilot-clm path   --state-dir /path/from/banner
copilot-clm off   --state-dir /path/from/banner
copilot-clm on    --state-dir /path/from/banner
copilot-clm reset --state-dir /path/from/banner
```

These commands cost no model call and take effect on the next request. `off` sends
raw history; `on` restores the retained projection; `reset` discards the projection.
There is no custom `/clm` TUI panel in this release. Ask the model to compact its
CLM mirror to request an immediate edit. Copilot's own `/compact` remains available.

## Budget, recovery and records

- The default target is **64,000 estimated tokens including instructions and tools**.
  The estimate is serialized UTF-8 JSON bytes divided by 3, not an exact tokenizer
  count. At 75%, a short notice encourages editing. Actual provider usage is saved
  separately; no cache or dollar savings are claimed.
- Above budget, an overflow guard replaces old textual tool outputs with pointers
  to complete `spills/*.json` files. It preserves the newest two history items,
  original user messages, calls and opaque content. If the remaining request still
  exceeds budget, it returns a clear error. Increase `--budget`, edit the mirror
  from another terminal, or use native `/compact`. `--no-guard` disables spilling,
  but still enforces the target budget.
- Native auto-compaction remains enabled. A rewrite/shortening of raw history,
  a model/system/tool-schema change or a divergent branch creates a fresh stream
  instead of applying an unrelated projection. Utility calls without tools pass
  through without consuming edits. CLM cannot prevent native compaction based on
  Copilot's internal accounting of the raw history.
- Resume both layers explicitly:
  `copilot-clm run --state-dir /previous/state -- --resume SESSION_ID`.
  The projection is reused only for a matching raw prefix and request
  configuration. A changed configuration safely starts a new stream.
  Matching tolerates Copilot reserializing JSON tool arguments on resume;
  conversation text and other wire fields must match exactly.
  Concurrent launchers cannot share a state directory. Subagent conversations
  with different histories/configurations get separate mirrors. Native
  `prompt_cache_key` values also distinguish live scopes; a restarted launcher can
  rebind a new key to a unique persisted history. Ambiguous matches start fresh.
  This is best-effort matching: conversations with identical histories and no
  distinct cache key cannot be distinguished. Use separate launchers/state roots
  when independent sessions require isolation; multi-agent sharing is experimental.
- The default state root is `$XDG_STATE_HOME/copilot-clm/<run-id>` or
  `~/.local/state/copilot-clm/<run-id>`. Each stream keeps `context.json`,
  `status.json`, the latest effective `input.json`, durable `state.json`, edit
  revisions and per-request usage. `--record-requests` additionally saves each
  effective request. Tool-less utility calls, including native summaries, have
  their own `utility/usage/` records; count these when auditing cost.
  The adapter records only usage returned by the provider. Missing usage or a
  disk-write warning means accounting is incomplete. Directories/files use
  `0700`/`0600` and atomic writes.
  These files contain conversation/code data and persist for resume/audit;
  delete the run directory when no longer needed. They are not a security boundary
  against the same user or an agent with shell access.
- Full-history HTTP requests are required. `previous_response_id`, server-side
  `conversation` state and WebSocket continuation are unsupported. Mid-history
  system/developer messages are rejected rather than moved. This version does not
  implement the Pi/OpenCode panel, their full configuration surface, learned
  context policies, or prompt-injection-proof memory.
- Configure the final provider URL: redirects are rejected so credentials cannot
  follow them to another host. SIGINT/SIGTERM to the launcher stop its native child.
- Editing a prefix can reduce prompt-cache hits and cause a provider to discard
  persisted reasoning. Keeping opaque items in the payload does not guarantee
  that the service will reuse them. See the provider's
  [reasoning documentation](https://developers.openai.com/api/docs/guides/reasoning).

## Verification

```bash
python3 -m unittest discover -s tests -v
# Native CLI plus scripted local provider; no paid model calls:
PYTHONPATH=. python3 tests/native_smoke.py
PYTHONPATH=. python3 tests/native_smoke.py --wire-api completions
# Explicit opt-in: real model calls, uses the configured gateway and Copilot quota:
PYTHONPATH=. python3 tests/live_smoke.py /absolute/path/to/new/evidence-directory --model YOUR_MODEL
```

The native test has Copilot execute a shell command, edit the resulting mirror,
and checks that the next provider request sees the edited output while Copilot's
stored transcript retains the original. Both wire formats also test CLI restart
and session resume. The opt-in live smoke requires an explicit context edit to
check the mechanism; it does not test whether the model chooses to compact well
on its own. These are integration tests, not efficiency benchmarks. See
[verification details](docs/verification.md) for tested versions and results.

## Attribution and license

The code and model-facing instructions in this repository are an independent
implementation under the MIT license. Pi/OpenCode and the CLM paper informed the
design; their source and prompts are not copied into this package. The original
[paper](https://arxiv.org/abs/2609.37725) and
[research implementation](https://github.com/facebookresearch/context-language-models)
retain their own licenses. This community project is not affiliated with GitHub
or Meta and does not relicense their code or research artifacts.
