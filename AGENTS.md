# AGENTS.md — Epoxy Working Guidelines

## How to work
- Set up a working environment first: editable install plus dev tools (pytest, ruff, mypy), and get a green baseline (`pytest`, `ruff check`, `ruff format --check src tests`, `mypy`) before changing code.
- Work in small iterations — one focused step at a time. One change per commit set; confirm with the user before expanding scope.
- A step is done only when `pytest`, `ruff check`, `ruff format --check src tests`, and `mypy` are all green. Verify before each commit and before finishing. CI runs the same four, so a step that skips `ruff format` locally can still fail the build.
- Commit after each successful step. Small, frequent commits; never one big commit at the end.
- Re-review the user's prompt before finishing and confirm every request was addressed. If something was deferred, state it clearly in the progress report summary.

## Code quality
- Keep code clean, clear, compact, and consistent. Remove dead/obsolete code, update stale comments, refactor, and reuse shared helpers at every iteration.
- Python 3.10+ with full type annotations (`mypy strict`); ruff line-length 100.
- No need for backward compatibility — it's OK to make migrations and break old formats when it improves the code.
- You may update packages/dependencies to their latest versions and install new tools when needed; re-run the full suite after any upgrade, and don't upgrade mid-task unless needed. After any dependency change, re-run `uv lock` so CI (`uv sync --frozen`) stays reproducible.

## Behavior changes travel together
- A behavior change ships with tests + README + version bump in the same change set. README documents the external contract (dockerstrator consumes `ls`/`status --json`).
- Machine-readable JSON schemas are additions-only — never removals or renames. Exit codes stay `0` success / `1` operational failure / `2` usage. User errors must be friendly (`ClickException`/`UsageError`), never tracebacks. Keep single-instance output stable when adding fan-out features.

## Tests
- Keep tests hermetic: no live docker or network. Stub via `monkeypatch` and keep `~/.cache` untouched (the `conftest.py` isolated-dirs pattern).

## Secrets
- Never log secrets; mask key/password/token values (the `SENSITIVE_KEY_PARTS` pattern).

## Versioning
- The project version lives in `src/epoxy/version.py` (`__version__`) and `pyproject.toml` (`[project] version`) — they MUST always match, and `epoxy --version` must report exactly that.
- Bump both together on ANY code change; `importlib.metadata` is not used, so drift breaks the contract silently. Docs-only changes (README, AGENTS.md) don't need a bump. Smoke-check with `epoxy --version` after bumping.
