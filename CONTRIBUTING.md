# Contributing to Cahoot

Thanks for the interest — Cahoot is small enough that one well-targeted PR can move the project a phase forward. Before you start, read `CLAUDE.md` (the build plan) and `docs/ARCHITECTURE.md` (the rationale). Most "why didn't you just …" questions are answered there.

## Dev setup

```bash
git clone <your-fork-url>
cd cahoot
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install   # optional but recommended
```

Requires **Python 3.11+** and **tmux 3.0+** to actually run the app.

### macOS + Python 3.13 gotcha

`python -m venv` on macOS marks `.venv/` as hidden (`UF_HIDDEN`), and Python 3.13's `site.py` now skips `.pth` files that inherit that flag. The hatchling editable install drops a `_editable_impl_cahoot.pth` into the venv that gets skipped, so `cahoot` and `python -m cahoot` (without `PYTHONPATH`) both fail with `ModuleNotFoundError: No module named 'cahoot'`.

One-time fix after install:

```bash
chflags -R nohidden .venv
```

Re-run after any `pip install -e .` or `pip install -e ".[dev]"`. Affects local dev only — CI on Ubuntu/macOS works without it because it doesn't keep a long-lived `.venv`.

## The four gates

A PR is ready when these all pass locally:

```bash
ruff format .
ruff check .
mypy cahoot
pytest --cov=cahoot
```

CI runs the same set on Ubuntu + macOS, Python 3.11 + 3.12. Failing any one blocks merge.

Coverage shouldn't drop below 80% on touched modules. If you're deleting a feature, also delete its tests — don't leave them as historical curios.

## Architectural invariants

`CLAUDE.md` §3 lists ten invariants that were chosen deliberately. If your PR touches any of them, the description must explain which one and why the change preserves (or evolves) the intent. The most commonly bumped:

- Envelopes are frozen. Mutating one is undefined behaviour — construct a new one.
- Adapters depend on the `Bus` protocol, never on `InMemoryBus`.
- All inbound traffic goes through `_publish_from_agent`, not `bus.publish`.
- Async-only. No `time.sleep`, no `threading`, no blocking IO on the loop.

## What's in scope

The README, CLAUDE.md, and `docs/ARCHITECTURE.md` define scope. In short: TUI mission-control over tmux/SSH, single host, single operator, single SQLite file. Anything that pushes toward "agent OS" or "web dashboard" is out of scope and will be politely declined — see `CLAUDE.md` §10 for the full list.

## Adding an adapter

Read `docs/ADAPTERS.md` first — it walks through the four-method contract with a worked example. The integration path is:

1. Subclass `AgentAdapter` in `cahoot/adapters/<name>.py`.
2. Register in `cahoot/adapters/__init__.py:REGISTRY`.
3. Write integration tests in `tests/test_<name>_adapter.py`.
4. Add a worked `[[agents]]` block to `docs/examples/cahoot.toml`.

## Commit and PR style

- Conventional small commits welcome but not enforced. Subject lines under 70 chars, imperative voice.
- One logical change per PR. If you find yourself writing "also fixed …" in the body, that's two PRs.
- Reference the relevant build phase from `CLAUDE.md` §6 in the PR description so reviewers can map the work to the plan.

## Reporting bugs

Open an issue with:

- What you did (`tmux new-session …`, config snippet, command typed).
- What you expected.
- What happened (exception, log excerpt — `tail ~/.local/state/cahoot/cahoot.log`).
- Versions: `cahoot --version` (when available), Python, tmux, OS.

## Code of conduct

Be kind. Disagree with the idea, not the person. We're all here because we like terminals.
