# Verification of 0.1.0

Executed on 2026-11-21 with Python 3.12.3, Linux and GitHub Copilot CLI 1.0.114.
The [machine-readable record](verification.json) includes per-request usage and
SHA-256 hashes of the implementation and test scripts. Raw session state and
provider requests are not included in the public repository.

| Check | Result |
|---|---|
| Unit, HTTP and launcher tests | 32 passed |
| Native CLI + scripted Responses provider | 4 requests, 1 accepted edit, resume passed |
| Native CLI + scripted Chat Completions provider | 4 requests, 1 accepted edit, resume passed |
| Native CLI + real gateway, model ID `gpt-6.2-sol` | 6 requests, 1 accepted edit, final retained fact correct |
| Build and isolated wheel installation | Passed; installed executable reports 0.1.0 |

The scripted tests use the actual Copilot binary and its actual bash tool. The
provider asks it to emit a sentinel, edit the corresponding tool output in
`context.json`, and finish. Assertions inspect the next upstream request and the
original Copilot transcript. They then restart the launcher and resume the same
native session, checking that the projection survives. No paid model calls are
made in those tests.

The live test uses a configured OpenAI-compatible gateway. `gpt-6.2-sol` is the
gateway model identifier; this test does not independently attest the underlying
model. Chat Completions was tested with the native CLI and scripted provider, not
with a paid model in this review. Native CLI execution was tested on Linux only;
CI separately exercises the Python tests and build on Linux and macOS.
The live trace below was captured at commit `1f9698f`; the subsequent fix removes
reverse DNS from loopback listener startup and leaves request projection unchanged.

## Live trace

The prompt explicitly asks the model to read a fixture, replace its obsolete log
in the context mirror, check the accepted revision, and report the retained fact.

| Request | Action produced | Input tokens | Cache-read tokens |
|---|---|---:|---:|
| 1 | Read the fixture with bash | 5,334 | 2,014 |
| 2 | Inspect the mirror using `python`; command unavailable | 6,694 | 5,331 |
| 3 | Retry inspection with `python3` | 6,875 | 6,691 |
| 4 | Edit only the identified tool result | 7,068 | 6,872 |
| 5 | Read status; revision 1 accepted, edited result already in request | 6,036 | 5,331 |
| 6 | Return the correct retained fact | 6,304 | 6,033 |

The selected tool output shrank from 4,970 to 50 characters. Requests 5 and 6
contained the replacement and no longer contained the original verbose tool
result. Copilot's own transcript retained the original. The failed `python` call
is included in the request counts and usage, not discarded.

This demonstrates that an edit reaches the next real model request. It does not
measure autonomous compaction quality or prove dollar savings. There is no
ordinary-harness control here. Editing also reduced cache reuse at request 5.
The provider may invalidate persisted reasoning when its prefix changes; see
[OpenAI's reasoning documentation](https://developers.openai.com/api/docs/guides/reasoning).

## Changes made during review

- Separate live native cache-key scopes and reject ambiguous restored-history
  matches, while allowing an unambiguous resume to rebind its changed cache key.
- Keep a budget notice from pushing an otherwise fitting request over its limit.
- Give model-created notes the assistant role and restore their authorship label
  after every edit.
- Keep opaque reasoning and the rest of its turn structurally together while
  allowing textual tool outputs to be shortened.
- Include tool-less utility requests in usage accounting; tolerate usage-file
  failures without cutting off the provider's stream.
- Reject provider redirects, avoid passing provider secrets in the child
  environment, and return a proper error on an upstream connection failure.
- Reap the native child when the launcher receives SIGINT/SIGTERM, release the
  state lock, and bound reads of rejected mirror files.
- Bind the private loopback listener without reverse DNS, after macOS CI exposed
  a startup delay in the standard HTTP server's hostname lookup.

The first four regression cases added during review failed on the original
adapter: cross-cache-session edits, ambiguous recovery, budget notices and note
authorship. They pass in this version. Additional tests cover the other changes.

## Reproduce

From the repository root:

```sh
python3 -m unittest discover -s tests -v
PYTHONPATH=. python3 tests/native_smoke.py
PYTHONPATH=. python3 tests/native_smoke.py --wire-api completions
```

For an opt-in live run, configure your upstream and key as described in the
[README](../README.md), then choose a new output directory:

```sh
PYTHONPATH=. python3 tests/live_smoke.py /absolute/new/evidence-directory --model YOUR_MODEL
```

Live runs incur provider usage. The test uses a dedicated Copilot home and
workspace, disables MCP servers, and permits only the native bash tool. Its
auto-approval flags apply to the test fixture; the launcher does not enable them
for normal use.
