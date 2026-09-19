# Code Review: Gluetun VPN Manager CLI (`vpn`)

**Reviewer:** Ling 3.0 Flash Fin Free
**Date:** 2026-09-19
**Commit reviewed:** `4532c2b` ("Document control-port resolution, bench winner no-op, and recreate verification")
**Version reviewed:** 0.2.6 (`src/vpn/version.py` == `pyproject.toml` == `vpn --version`)
**Repo:** `/workspace`
**Language:** Python 3.10+, click, prompt-toolkit, rich, docker compose

---

## Executive Summary

The codebase is mature, well-tested (222 tests, all passing), and passes all static analysis (ruff, mypy). The architecture is clean: a CLI layer (`cli.py`) orchestrates a runtime configuration engine (`apply.py`), a Docker abstraction (`docker.py`), a provider registry (`providers.py`), and resilient IP probing (`ipinfo.py`). However, beneath the clean surface, there are several subtle bugs, design inconsistencies, and improvement opportunities. Below is a prioritized assessment.

---

## Critical Bugs & Logic Errors

### 1. `read_env_file` does not strip inline comments (Bug)

**Location:** `src/vpn/config.py`, `read_env_file()` (lines 77–100)

```python
if line.startswith("export ") or line.startswith("export\t"):
    line = line[7:].lstrip()
if "=" not in line:
    continue
key, value = line.split("=", 1)
value = value.strip()
```

A line like `KEY=value # comment` is parsed with the value being `"value # comment"` instead of `"value"`. The function only skips lines that **start** with `#`, but `.env` files commonly use inline comments. This means a credential value accidentally containing `#` (e.g., a base64 string with padding) would be corrupted, or an intentional inline comment would be silently included in a secret value.

**Fix:** Strip inline comments from the value portion, or at minimum reject/quote them. A simple approach: after extracting the value, split on `#` with `value = value.split("#")[0].strip()` (being careful that `#` could be a legitimate character in some values, though unlikely in `.env` files).

### 2. `_test_one` cleanup attempts to remove containers that were never launched (Minor Resource Leak)

**Location:** `src/vpn/bench.py`, `_test_one()` (lines 327–344)

```python
def _test_one(candidate: Candidate, name: str, size_mb: int, timeout: int) -> _ParallelResult:
    if not launch_container(name, _bench_container_env(candidate)):
        return _ParallelResult(error="container launch failed")
    try:
        ...
    finally:
        remove_container(name)
```

When `launch_container` returns `False`, the function returns immediately, but the `finally` block in the calling `_test_batch` / `_test_one` structure still attempts `remove_container(name)`. Looking more carefully, `_test_one` itself has a `finally: remove_container(name)` — but this runs even when `launch_container` failed and no container exists. `remove_container` calls `docker rm -f` which is idempotent (won't raise), but it's an unnecessary Docker API call for a container that never existed.

**Fix:** Wrap `remove_container` in a conditional, or move it inside the `try` block after verifying launch succeeded.

### 3. `print_ip_status` returns `True` when VPN IP is empty string (Logic Error)

**Location:** `src/vpn/ipinfo.py`, `print_ip_status()` (line 372)

```python
return vpn_ip != bare if bare else True
```

If `fetch_ip_info` somehow returns a result where `info["ip"]` is `""` (empty string), then `vpn_ip = ""`. The function returns `"" != bare` which evaluates to `True` (assuming `bare` is not empty), incorrectly reporting a verified connection when no IP was actually observed. This edge case shouldn't normally occur because `fetch_ip_info` rejects responses without an `ip` field, but the defense-in-depth is missing.

**Fix:** Add an explicit check: `return bool(vpn_ip) and vpn_ip != bare` or return `False` when `not vpn_ip`.

---

## Design Issues & Inconsistencies

### 4. Global `DEBUG` variable (Code Smell)

**Location:** `src/vpn/cli.py`, line 75 and `main()` (line 327)

```python
DEBUG = False
...
def main(debug: bool) -> None:
    global DEBUG
    DEBUG = debug
```

This module-level global makes `_log_env()` depend on mutable state. It cannot be tested cleanly without side effects, and it's not thread-safe. The `DEBUG` flag should be a `ContextVar` or passed as a parameter to functions that need it. Alternatively, it could be an attribute on the click Context object.

**Fix:** Replace with `contextvars.ContextVar("vpn_debug", default=False)` or pass `debug` through function parameters.

### 5. `current_instance()` recomputes on every call (Performance Issue)

**Location:** `src/vpn/instance.py`, `current_instance()` (lines 221–224)

```python
def current_instance() -> Instance:
    active = _active.get()
    return active if active is not None else default_instance()
```

Every call to `current_instance()` outside an `instance_context` triggers `resolve_instance(None)` → `required_name(None)` → `os.getenv("GLUETUN_INSTANCE")` → reads `.env` via `build_env()`. This is called frequently throughout the codebase (in `docker.py`, `control.py`, `ipinfo.py`, `speedtest.py`). Reading `.env` and constructing an `Instance` on every call is wasteful.

**Fix:** Consider caching the result of `default_instance()` with a simple memoization, or at minimum cache the env lookup. Alternatively, note that `current_instance()` is called inside `with instance_context(inst)` in most CLI commands, so the active context is usually set — the problem is mainly in `docker.py` functions like `container_env()`, `container_status()` etc. that call `current_instance()` directly.

### 6. `base_url()` reads `HTTP_CONTROL_SERVER_ADDRESS` from env — potential security issue

**Location:** `src/vpn/control.py`, `base_url()` (lines 36–44)

```python
address = env_lookup("HTTP_CONTROL_SERVER_ADDRESS")
return (address or current_instance().base_url).rstrip("/")
```

If a malicious or accidental `HTTP_CONTROL_SERVER_ADDRESS` is set in the environment, all control-server traffic (including settings mutations with API keys) would be sent to that address. This is documented in `base_url()` as "the historical escape hatch," but there's no validation or warning.

**Fix:** At minimum, validate that `HTTP_CONTROL_SERVER_ADDRESS` starts with `http://127.0.0.1:` or `http://localhost:`. Or remove this escape hatch entirely since all commands now target per-instance ports.

### 7. `up` command has confusing `requested` override logic

**Location:** `src/vpn/cli.py`, `up()` (lines 381–394)

```python
requested = any(v is not None for v in (provider, protocol, country, city))
...
if created:
    requested = country is not None or city is not None
```

After the container is created, `requested` is replaced with only country/city checks. This means passing `--provider surfshark --country Japan` to `vpn up` on a stopped container creates the container with surfshark (via compose) and only hot-swaps the country. This behavior is correct but the dual-use of `requested` is confusing and fragile — a reader might expect `--provider` to also trigger a hot-swap.

**Fix:** Rename the second `requested` to something like `location_requested`, or extract the logic into a clearly-named helper function with comments explaining the two phases.

### 8. Inconsistent error handling: `SystemExit` vs `click.UsageError` vs `RuntimeError`

**Location:** Throughout `cli.py`, `providers.py`, `instance.py`, `apply.py`

The codebase mixes three error-raising patterns:
- `raise SystemExit("message")` — used in `cli.py`, `providers.py`, `instance.py`
- `raise click.UsageError("message")` — used in `instance.py` for name validation
- `raise RuntimeError` — indirectly via `ControlError` in `control.py`

`SystemExit(1)` in commands is intentional for the exit-code contract. But `SystemExit("message")` (without a code) exits with code 0, which is incorrect for error paths. Looking at `cli.py`:

```python
raise SystemExit("Cannot read runtime settings — is gluetun's control server reachable?")
```

This exits with code 0, not 1, because `SystemExit` with a string argument defaults to code 0. The documented contract says exit code 1 for "VPN failed / leak."

**Fix:** Always use `raise SystemExit(1)` or `raise SystemExit(1, "message")` for error paths. `click.UsageError` is already used correctly for usage errors (exit code 2).

### 9. `container_env()` returns empty `{"": ""}` on docker unavailability (Edge Case)

**Location:** `src/vpn/docker.py`, `container_env()` (lines 77–85)

```python
def container_env() -> dict[str, str]:
    out = inspect_container("{{range .Config.Env}}{{println .}}{{end}}")
    env: dict[str, str] = {}
    for line in (out or "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            env[key] = value
    return env
```

If `inspect_container` returns `None` (docker unavailable), `out or ""` gives `""`, `"".splitlines()` gives `[]`, so the loop doesn't execute. This is fine. But if `out` is an empty string `""`, same thing. However, if `out` is `"\n"`, `splitlines()` gives `["", ""]`, and the loop processes empty lines — `line.partition("=")` gives `("", "", "")`, so `sep = ""` is falsy and the line is skipped. OK, no bug here.

---

## Subtle Bugs & Edge Cases

### 10. `_vote` tie-breaking in `ipinfo.py` has a latent issue

**Location:** `src/vpn/ipinfo.py`, `_vote()` (lines 190–217)

```python
best_entries: list[tuple[str, dict[str, object]]] = []
best_priority = len(_PROVIDERS)
for _ip, entries in by_ip.items():
    priority = min(order[name] for name, _ in entries)
    if len(entries) > len(best_entries) or (
        len(entries) == len(best_entries) and best_entries and priority < best_priority
    ):
        best_entries, best_priority = entries, priority
```

On the first iteration, `best_entries = []`, so `len(best_entries) = 0`. For any IP group with 1+ entries, `len(entries) > 0` is True, so it's selected. But the condition `best_entries and priority < best_priority` in the second clause short-circuits when `best_entries` is empty (falsy), which is correct — we only enter the tie-breaker when both groups have the same size AND the best_entries is non-empty. This works correctly.

However, if two IP groups have the **same** number of entries and the **same** priority (e.g., both have one entry from the same provider), the first one encountered wins. But since `by_ip` is built from a dict iteration, the order of providers in `_PROVIDERS` is already reflected in the `order` dict, so the priority comparison handles this. This is correct but fragile.

### 11. `_parse_servers_output` hardcodes column indices as fallback

**Location:** `src/vpn/servers.py`, `_parse_servers_output()` (lines 54–93)

```python
country_idx = idx.get("country")
city_idx = idx.get("city")
if country_idx is None or city_idx is None:
    country_idx, city_idx = 1, 2
```

If the markdown header doesn't contain "country" or "city" columns (e.g., a custom server list format), the fallback uses columns 1 and 2 (0-indexed after splitting by `|`). But column 1 is the first data column after the leading `|`. If the format changes (e.g., a leading "Name" column is added), the fallback would be wrong. This is a fragile assumption.

**Fix:** Make the fallback more robust or document the assumed format clearly.

### 12. `_container_ids` in `bench.py` uses `itertools.count()` — potential collision in long-running processes

**Location:** `src/vpn/bench.py`, line 56

```python
_container_ids = itertools.count()
```

The container name is `f"{_CONTAINER_PREFIX}{os.getpid()}-{next(_container_ids)}"`. If `_container_ids` overflows or wraps (extremely unlikely for `itertools.count()` in CPython), or if a process runs millions of bench iterations, names could collide. In practice, this is not a real concern since `itertools.count()` uses Python integers (arbitrary precision).

**Fix:** No fix needed, but worth noting.

### 13. `deploy` command's `compose()` function always calls `current_instance()`

**Location:** `src/vpn/docker.py`, `compose()` (lines 112–128)

```python
def compose(*args, env_overrides=None, timeout=None):
    inst = current_instance()
    cmd = ["docker", "compose", "-f", inst.compose_file, "-p", inst.project, *args]
```

This function doesn't accept an `Instance` parameter — it always uses `current_instance()`. This is fine when called inside `instance_context()`, but if `compose()` is ever called outside a context manager (e.g., in a new thread), it would resolve the instance independently. Currently, all callers use `instance_context`, so this is not a bug, but it's a tight coupling that could become one.

**Fix:** Consider adding an `inst` parameter to `compose()` with a default of `current_instance()` for flexibility.

---

## Potential Improvements

### 14. Extract shared "resolve instance and check running" pattern in CLI commands

**Location:** `cli.py` — `up`, `connect`, `bench`, `status`, `ls`, `down`, `logs`, `dns`, `update`

Every command follows the same pattern:
```python
inst = _resolve_for_command(instance)
with instance_context(inst):
    ...
```

This is well-factored through `add_instance_options` and `_resolve_for_command`. However, `require_api_key()` is called in some commands but not others (correctly — read-only commands don't need it). The pattern is clean but could be further reduced with a decorator.

**Improvement:** Create a `@require_api_key` decorator that wraps commands needing it, removing the `require_api_key()` calls from each command body.

### 15. Add `__all__` to modules for explicit API contracts

**Location:** `src/vpn/apply.py`, `src/vpn/control.py`, `src/vpn/providers.py`, `src/vpn/servers.py`, `src/vpn/latency.py`, `src/vpn/speedtest.py`

Only `__init__.py` has `__all__`. Modules that expose internal helpers (like `_probe`, `_vote` in `ipinfo.py`) don't restrict imports, making it unclear what's public API. The README documents a "Consumer API" contract for `dockerstrator`, so explicit `__all__` would help maintain that contract.

**Improvement:** Add `__all__` to each module, especially `apply.py`, `control.py`, and `providers.py`.

### 16. Test coverage gaps

Several areas lack tests:
- `_parse_servers_output` with malformed headers (the fallback path)
- `container_env()` returning edge cases
- `_log_env` with no `DEBUG` flag and sensitive keys (covered)
- `_test_one` failure path when `launch_container` returns False (not directly tested)
- `_status_doc` when `state == "starting"` (the `drift` computation path)
- Error handling in `_request` for `TimeoutError` and `URLError` (only tested via `ControlError` propagation)

**Improvement:** Add tests for the above paths, especially the `starting` state in `_status_doc` and the `_test_one` failure path.

### 17. `vpn.yml` template uses hardcoded `127.0.0.1:8000:8000/tcp`

**Location:** `src/vpn/vpn.yml`, line 10

```yaml
ports:
  - "127.0.0.1:8000:8000/tcp"
```

The port is hardcoded in the template and replaced by `render_compose()`. If the template is ever updated without updating the regex in `render_compose()`, the substitution would fail silently. Also, the `HTTP_CONTROL_SERVER_ADDRESS=:8000` inside the compose file is hardcoded — but `render_compose` doesn't replace this. The `container_control_port()` function reads the actual published port from Docker inspect, and `_resolve_for_command` corrects the `Instance.control_port`. So this works, but the hardcoded `:8000` in the template is a source of confusion.

**Improvement:** Make the template use a placeholder like `{{PORT}}` and render it properly, or update `render_compose` to also replace `HTTP_CONTROL_SERVER_ADDRESS=:8000`.

### 18. `countries.py` `COUNTRY_NAMES` is a large dict — could use a data file

**Location:** `src/vpn/countries.py` (lines 5–255)

250+ lines of country code → name mappings. This is a static data file that could be externalized. It also makes the module large and harder to update.

**Improvement:** Move to a JSON file in `src/vpn/data/countries.json` or keep it in code but add a comment explaining it's ISO 3166-1 data. Alternatively, generate it from a standard source.

### 19. `picker.py` uses `class:` style prefixes — verify prompt_toolkit compatibility

**Location:** `src/vpn/picker.py`, `_body()` and `_segments()` (lines 215–230)

Styles are applied as `"class:selected"` and `"class:match"`. These correspond to style names defined in `_STYLE` (`"selected": "bold reverse"`, `"match": "bold ansiyellow"`). In prompt_toolkit, `class:X` applies the style named `X`. This should work, but it's an unusual pattern and could break if prompt_toolkit changes its class-style resolution.

**Improvement:** Consider using direct style names without the `class:` prefix for simplicity, e.g., `"selected"` instead of `"class:selected"`.

### 20. `bench.py` `run_bench` has complex control flow with `nonlocal`

**Location:** `src/vpn/bench.py`, `run_bench()` (lines 163–301)

The `test_stage` nested function uses `nonlocal current, prev_ip`, and the overall function has multiple try/except blocks with complex restoration logic. While the code is correct, it's difficult to follow. The `connect_winner` and `interrupted` paths interact in subtle ways.

**Improvement:** Refactor `run_bench` into smaller functions: `_run_serial_stage`, `_run_parallel_stage`, `_determine_winner`, `_apply_winner`. This would improve readability and testability.

---

## Prioritized Work Plan

### Priority 1 — Fix Bugs (immediate)

| # | Issue | Effort | Impact |
|---|-------|--------|--------|
| 1 | `read_env_file` inline comment stripping | Small | Security/data integrity |
| 8 | `SystemExit` without exit code on error paths | Small | Correct exit codes per documented contract |
| 3 | `print_ip_status` returns True for empty VPN IP | Small | Correctness |

### Priority 2 — Fix Design Issues (near-term)

| # | Issue | Effort | Impact |
|---|-------|--------|--------|
| 4 | Replace global `DEBUG` with `ContextVar` | Small | Testability, thread-safety |
| 5 | Cache `current_instance()` / `build_env()` | Small | Performance (called very frequently) |
| 6 | Validate `HTTP_CONTROL_SERVER_ADDRESS` | Small | Security |
| 2 | Fix `_test_one` container cleanup on launch failure | Small | Resource hygiene |

### Priority 3 — Refactoring & Maintainability

| # | Issue | Effort | Impact |
|---|-------|--------|--------|
| 7 | Rename clarify `requested` override in `up()` | Small | Readability |
| 10 | Document/fix `_vote` tie-breaking | Small | Robustness |
| 17 | Fix hardcoded port in `vpn.yml` template | Small | Maintainability |
| 19 | Simplify `class:` style prefixes in picker | Small | Readability |

### Priority 4 — Test Coverage & Quality

| # | Issue | Effort | Impact |
|---|-------|--------|--------|
| 16 | Add tests for `starting` state, `_test_one` failure, `_request` errors | Medium | Coverage |
| 13 | Decouple `compose()` from `current_instance()` | Medium | Flexibility |
| 15 | Add `__all__` to modules | Small | API clarity |

### Priority 5 — Nice-to-Have

| # | Issue | Effort | Impact |
|---|-------|--------|--------|
| 11 | Robust column fallback in `_parse_servers_output` | Small | Robustness |
| 14 | Add `@require_api_key` decorator | Small | DRY |
| 18 | Externalize country data | Medium | Maintainability |
| 20 | Refactor `run_bench` control flow | Medium | Readability |
| 9 | `container_env()` edge cases | Trivial | Defensive |

---

## Summary

The `vpn` CLI is a well-engineered tool with strong test coverage and clean architecture. The most impactful issues are the `read_env_file` inline comment bug (a data integrity / security concern), the `SystemExit` without exit code (violating the documented exit-code contract), and the global `DEBUG` variable (a testability concern). The design is sound but has some tight coupling through `current_instance()` that could benefit from caching and decoupling. The bench module's control flow is the most complex part of the codebase and could benefit from refactoring into smaller functions. Overall, this is a high-quality codebase with room for targeted improvements rather than any fundamental redesign.
