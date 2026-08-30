# Contributing

Thanks for looking. This project has a strong opinion about where knowledge is
allowed to live, and most review comments come back to it, so it is worth
stating up front.

## The one rule

**Only `gateway/llm_gateway.py` knows that providers exist.** Everything above
it speaks in terms of "a worker with an id, a model string and a credential".
If a change teaches the orchestrator, the compiler, the reconciler or the
storage layer the name of a provider, that change is in the wrong module.

Two rules follow from it:

- **Workers never receive a transcript.** They receive a compiled context
  package. `raw_conversation_transfers` is asserted to be zero in the tests, and
  it is the number the whole design exists to keep at zero.
- **A worker's claims are not facts.** They pass through the reconciler before
  they touch canonical state. New claim types get new reconciliation, not a
  shortcut into `ExecutionState`.

## Setup

```bash
git clone https://github.com/VivanRajath/context-orchestration-engine.git
cd context-orchestration-engine
python -m venv .venv
.venv/Scripts/activate        # Windows;  source .venv/bin/activate elsewhere
pip install -e ".[dev]"
```

Python 3.11 or newer.

## Before you open a pull request

```bash
python -m pytest -q                       # the full suite
python -m pyflakes src tests main.py      # no unused imports or names
```

The suite runs entirely offline against the mock gateway — no key, no network,
no cost. If a change cannot be tested that way, it probably belongs behind the
gateway seam.

Add a test with a behaviour change. The existing files are the guide:
`tests/test_reconciler.py` for the trust boundary, `tests/test_context_compiler.py`
for budget and relevance, `tests/test_storage.py` for persistence and resume.

## Style

Match the surrounding code rather than a linter's idea of it.

- Comments explain *why*, and are worth writing only where the reason is not
  obvious from the code. Several modules have a header comment explaining the
  module's job; keep it accurate.
- Type hints on public functions; `from __future__ import annotations` at the
  top.
- Pydantic models for anything crossing a boundary — a worker's raw claims and
  the canonical records they become are deliberately different types.
- No new top-level import names: everything lives under `context_orchestration`,
  and a test asserts it.

## Things that are welcome

- **Switch policies.** `SequentialSwitchPolicy` is deliberately dumb.
  Cost-aware, capability-aware, rate-limit-aware and context-limit-aware
  switching are all implementations of the same `SwitchPolicy` protocol.
- **Storage backends.** `SQLiteStore` is one implementation of a narrow
  interface.
- **Token estimators.** `HeuristicTokenEstimator` and `TiktokenEstimator` show
  the shape.
- **Compiler scoring.** Relevance and prioritization are the part with the most
  headroom.

## Reporting a problem

Open an issue with the worker roster (redact the keys), the plan, whether the
run was mock or live, and what the reconciler said. A `--mock` reproduction is
worth ten paragraphs.

## Security

Do not open a public issue for a vulnerability. Email the address on the GitHub
profile instead.

By contributing you agree your work is licensed under the [MIT License](LICENSE).
