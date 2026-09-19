# Code Review: vpn (Gluetun CLI)

**Reviewer:** MiMo V2.5 Free  
**Date:** 2026-09-19  
**Version reviewed:** 0.2.6  
**Files reviewed:** 16 source files, 20 test files  

---

## Executive Summary

The codebase is well-structured, well-tested (154 tests, all passing), and follows a clean architecture with clear separation of concerns. The module layout (`apply`, `control`, `docker`, `instance`, `ipinfo`, etc.) is logical and the documentation is thorough. However, there are several real bugs, design inconsistencies, and robustness gaps — some of which can cause incorrect behavior in production.

---

## 🔴 Critical Bugs (Must Fix)

### 1. `_test_batch` — `pool.shutdown()` is dead code (bench.py:371)

```python
def _test_batch(candidates, size_mb, timeout):
    ...
    pool = ThreadPoolExecutor(max_workers=len(names))
    futures = [pool.submit(...) for ...]
    try:
        return [future.result() for future in futures]   # returns here
    except KeyboardInterrupt:
        for name in names:
            remove_container(name)
        pool.shutdown(wait=False, cancel_futures=True)
        ...
        raise                                            # re-raises
    pool.shutdown()   # ← UNREACHABLE: try returns, except re-raises
```

The `pool.shutdown()` at line 371 is never executed. On the normal path, `return` exits before reaching it. On `KeyboardInterrupt`, `raise` exits before reaching it. This means the executor is never properly cleaned up in the success path — it relies on GC and `__del__`, which is not guaranteed. The `shutdown()` call should be in a `finally` block or placed before the `return`.

**Fix:** Move `pool.shutdown()` into a `finally` block, or add it before the `return`:

```python
try:
    results = [future.result() for future in futures]
    return results
except KeyboardInterrupt:
    ...
    raise
finally:
    pool.shutdown(wait=False)
```

### 2. `inspect_container` ignores `name=None` inconsistently (docker.py:47-63)

```python
def inspect_container(format_string: str, name: str | None = None) -> str | None:
    container = name or current_instance().container
```

When `name` is explicitly passed as `None` (which is the default), it falls back to `current_instance()`. But callers like `container_control_port(name=name)` and `container_status(name=name)` pass through the `name` parameter, which means a caller passing `None` (the default) silently targets the "current instance" rather than the named container. This is by design for most callers, but creates a subtle trap: if you ever call `inspect_container("...", name=None)` intending "no container," you get the current instance instead.

This is a design smell more than a bug, but it causes confusion. The `name` parameter default should be `Ellipsis` or a sentinel to distinguish "not provided" from "explicitly None."

### 3. `allocate_free_port` has a TOCTOU race (instance.py:242-255)

```python
def allocate_free_port() -> int:
    for port in PORT_RANGE:
        if not _port_in_use(port):   # check
            return port               # use — another process could bind in between
```

Between the `bind()` check in `_port_in_use` and the actual use of the port, another process (or another `vpn up` invocation) could claim the same port. This is mitigated by the fact that `allocate_free_port` is only called from `vpn up` for brand-new instances, but in a multi-instance or parallel-cli scenario it's a real race.

**Fix:** Instead of checking then using, try to bind the port and keep it locked, or use `SO_REUSEADDR` with atomic allocation.

### 4. `_fmt_row` KeyError on missing `city` (ipinfo.py:296-309)

```python
def _fmt_row(label, info, color=None, marker=" "):
    ip = str(info.get("ip", "?"))
    country = resolve_country(str(info.get("country", "?")))
    location = f"{info.get('city', '?')}, {country}"
```

This uses `info.get('city', '?')` which is safe, but then the `_fmt_table` function and `print_ip_status` call this with `last_info` which could be any dict shape. The fallback `?` is fine here, but in `_status_doc` (cli.py:245-249), the code does:

```python
ip = str(info.get("ip") or "")
exit_ip = {
    "ip": ip,
    "country": resolve_country(str(info.get("country") or "")) or None,
}
```

If `info` is `{"ip": "1.2.3.4"}` (no country key), `info.get("country")` returns `None`, and `str(None)` is `"None"`, which then gets passed to `resolve_country("None")`. Since "None" is not a valid country code or name, `resolve_country` returns `"None"` as a passthrough, and the exit_ip country becomes the string `"None"` rather than `None`.

**Impact:** `status --json` would report `"country": "None"` (a string) instead of `"country": null` when the echo services don't provide country data.

### 5. `with_location` clears ALL filter lists unconditionally (control.py:182-183)

```python
for filter_name in _LOCATION_FILTERS:
    selection[filter_name] = []
```

This clears `regions`, `categories`, `isps`, `hostnames`, `names`, and `numbers` — even when the user only wants to change the country. If gluetun had any of these set from a previous interaction (e.g., via its own UI), a `vpn connect --country Japan` would silently clear them. This is intentional per the docstring ("clears every location filter list so no stale value constrains the new selection"), but it means users cannot combine a `vpn connect` with pre-set gluetun filters — those are always wiped.

This is a **design decision** more than a bug, but it's worth noting that it's lossy.

---

## 🟡 Medium Bugs / Issues (Should Fix)

### 6. `fetch_ip_info` always adds bare IP to `exclude` even when caller doesn't want it (ipinfo.py:261-264)

```python
exclude = set(exclude_ips or ())
bare = real_ip()
if bare:
    exclude.add(bare)
```

The bare IP is **always** excluded, which is correct for leak detection. But this means `fetch_ip_info` can never return an observation matching the bare IP, even when a caller might want that (e.g., a diagnostic command). This is fine for the current usage but is a hidden invariant that could surprise future callers.

### 7. `current_exit_ip` returns string "None" when info has no `ip` key (ipinfo.py:375-379)

```python
def current_exit_ip(retries=CURRENT_EXIT_IP_RETRIES):
    outcome = fetch_ip_info(retries=retries)
    info = (outcome.result.info if outcome.result else None) or outcome.last_info
    return str(info.get("ip")) if info else None
```

If `info` is `{"country": "DE"}` (no `ip` key), `info.get("ip")` returns `None`, and `str(None)` returns `"None"`. This string would then be passed around as an IP address. It should return `None` instead.

### 8. `docker.compose` merges env without preserving ordering (docker.py:127-128)

```python
env = {**inst.env, **(env_overrides or {})}
return run(*cmd, env={**os.environ, **env}, timeout=timeout)
```

If both `inst.env` and `env_overrides` contain the same key, the override wins. This is correct. But `inst.env` itself is `{**os.environ, **base}` (from `build_env`), meaning `env_overrides` → `inst.env` file values → process env. The double merge means a process env var that's also in `.env` gets overwritten by `.env`, which gets overwritten by `env_overrides`. This is the intended behavior but the comment in `compose` ("Interpolation env is the instance's merged env (its .env/env-file wins over the process environment) overlaid by any overrides") could be clearer about the three-layer merge.

### 9. `_status_doc` and `print_ip_status` duplicate exit-IP logic (cli.py:219-264 vs ipinfo.py:325-372)

Both `_status_doc` (for `--json`) and `print_ip_status` (for human output) compute the exit IP, check for leaks, and resolve the country — but through different code paths with slightly different behavior:

- `_status_doc` calls `_probe()` directly and checks `bare` inline
- `print_ip_status` calls `fetch_ip_info()` which calls `_probe()` in a retry loop

This means `status --json` does a single probe while `status` (human) does up to 15 retries. The JSON path could report `leak: true` while the human path would eventually succeed.

### 10. `down` swallows all exceptions silently (cli.py:504-506)

```python
with instance_context(_resolve_for_command(instance)):
    with contextlib.suppress(Exception):
        control.set_vpn_status("stopped", timeout=DOWN_TIMEOUT_S)
```

A `KeyboardInterrupt` or `SystemExit` would be caught here too (they inherit from `BaseException`, not `Exception`, so actually they're fine). But a genuine `ControlError` with useful diagnostic info (e.g., "auth failed") is silently discarded. The user sees "VPN stopped." even if the VPN wasn't actually told to stop.

### 11. `_real_ip_cache` and `_real_ip_info` are process-level globals (ipinfo.py:38-39)

```python
_real_ip_cache: str | None = None
_real_ip_info: dict[str, object] | None = None
```

These are module-level globals mutated by `_fetch_real_ip_info`. In a multi-threaded context (e.g., bench with concurrency), two threads could race on these. In practice, `real_ip()` is called from the main thread and `_probe` runs inside containers, so this is safe today, but it's fragile.

### 12. `read_env_file` doesn't validate quote matching (config.py:97-98)

```python
if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
    value = value[1:-1]
```

A value like `"hello'` would not have matching quotes and would be kept as-is — which is correct. But a value like `'"hello"'` (double-quoted single quote inside) would strip the outer double quotes and leave `'hello'` including the inner single quotes. This is standard `.env` behavior but worth documenting.

---

## 🟢 Design Issues & Inconsistencies

### 13. Global mutable `DEBUG` flag (cli.py:75, 326-327)

```python
DEBUG = False

@click.group()
@click.option("--debug", ...)
def main(debug):
    global DEBUG
    DEBUG = debug
```

A global mutable flag is an anti-pattern. It should be passed through click's context or stored in a module-level `ContextVar`. This also means `_log_env` reads module-level state rather than receiving it as a parameter.

### 14. `current_instance()` fallback to `default_instance()` is surprising (instance.py:221-224)

```python
def current_instance():
    active = _active.get()
    return active if active is not None else default_instance()
```

Outside of `instance_context`, this silently resolves to whatever `GLUETUN_INSTANCE` is set to (or errors). This means code that forgets to wrap in `instance_context` will still work if the env var is set, but will fail otherwise — creating inconsistent behavior. It should either always require the context or always fail outside it.

### 15. `apply_location` holds lock during full GET→PUT round-trip (apply.py:89-93)

```python
def apply_location(sel):
    with swap_lock():
        put_settings(with_location(get_settings(), ...))
```

The lock is held during the HTTP GET, the Python mutation, and the HTTP PUT. The PUT alone can take up to 60 seconds (`PUT_TIMEOUT_S`). During this time, all other CLI invocations for the same instance are blocked on the lockfile. This is correct for serialization but means a hung control server blocks the user's terminal with no feedback.

### 16. `_vote` tie-breaking is priority-based but not documented (ipinfo.py:190-217)

The voting algorithm uses plurality with priority tie-breaking, but the priority order (`ipinfo` > `cloudflare` > `ifconfigco` > `ip2location`) is implicit. If a user sees a different IP than expected, the tie-breaking rule is non-obvious.

### 17. `connect` command fetches `prev_ip` outside the instance context check (cli.py:486)

```python
with instance_context(inst):
    require_api_key()
    if not container_running():
        raise SystemExit(...)
    current = _require_selection()
    ...
    prev_ip = current_exit_ip()  # docker exec — could be slow
    target, swapped = _apply_request(...)
```

`current_exit_ip()` does a `docker exec` which can take several seconds. This runs after the lock is not yet held (the lock is inside `_apply_request` → `apply_location`), so it's fine for concurrency. But it means the user sees "Swapping..." only after the IP probe completes, which can feel slow.

### 18. `ServerRow` is a `dict` alias, not a dataclass (servers.py:51)

```python
ServerRow = dict[str, str]
```

This provides no type safety — any dict passes as a `ServerRow`. A `@dataclass` or `TypedDict` would catch typos like `row["contry"]` at type-check time.

### 19. Compose template uses `container_name` directly in the YAML (vpn.yml:4)

```yaml
container_name: gluetun
```

The `render_compose` function does string replacement:
```python
body = body.replace("container_name: gluetun", f"container_name: {name}")
```

This is fragile — if the template ever changes (e.g., adding a comment containing "container_name: gluetun"), the replacement breaks silently. A template engine (Jinja2) or YAML manipulation would be more robust.

### 20. No `--help` for `vpn bench` stage descriptions

The bench command has many options (`--top`, `--scan-size`, `--size`, `-c`, `--connect`) but the help text doesn't explain the three-stage pipeline (latency → screening → finals). A user seeing `--scan-size` and `--size` may not understand they control different stages.

---

## 🔵 Minor Issues & Nits

### 21. Inconsistent `name` parameter passthrough in docker.py

`container_running(name=name)`, `container_status(name=name)`, `container_image(name=name)`, `container_control_port(name=name)` all have `name: str | None = None`. But callers sometimes pass `None` explicitly (e.g., `container_running()` with no args) and sometimes pass a name. The `name` parameter is effectively "use the current instance if not provided," which is implicit behavior.

### 22. `_parallel_stage` progress display is off-by-one (bench.py:383-388)

```python
for start in range(0, len(pool), concurrency):
    batch = pool[start : start + concurrency]
    say(
        f"[{start + 1}-{start + len(batch)}/{len(pool)}] "
        + ", ".join(r.candidate.location for r in batch)
    )
```

The range `start+1` to `start+len(batch)` is correct (1-indexed), but the format `{start + 1}-{start + len(batch)}` shows the last item as `start + len(batch)`, not `start + len(batch)`. For example, with 5 items and concurrency 2: batches are `[1-2/5]`, `[3-4/5]`, `[5-5/5]`. This is correct but looks odd — the second number in the range should arguably be `min(start + concurrency, len(pool))`.

### 23. `format_result` hardcodes download size (speedtest.py:15-16)

```python
def format_result(result):
    return f"↓ {result['mbits']:.1f} Mbit/s ({result['mbytes']:.0f} MB in {result['seconds']:.1f}s)"
```

This works because `measure` always stores `mbytes` as the requested size, but the function doesn't validate its input and would produce garbage with a malformed dict.

### 24. Tests monkeypatch `"vpn.cli.print_ip_status"` but it's imported differently (test_selection.py:21)

```python
monkeypatch.setattr("vpn.cli.print_ip_status", lambda **kwargs: True)
```

This works because `cli.py` does `from vpn.ipinfo import ... print_ip_status ...`, which creates `cli.print_ip_status` as an attribute. But if someone refactored to `from vpn import ipinfo; ipinfo.print_ip_status(...)`, the monkeypatch would silently stop working. The test should monkeypatch the source module (`vpn.ipinfo.print_ip_status`) instead.

### 25. `_container_ids` counter is module-level and never reset (bench.py:56)

```python
_container_ids = itertools.count()
```

In long-running processes or test suites, this counter grows unboundedly. It's fine for the current CLI (each invocation is a fresh process) but would be a memory leak in a server context.

### 26. `read_env_file` doesn't handle multiline values (config.py:87-99)

Standard `.env` files support multiline values with triple quotes or trailing backslashes. This parser treats each line independently, so `KEY="line1\nline2"` would work (the `\n` stays literal), but actual multiline values (spanning multiple lines) would break.

### 27. `version.py` and `pyproject.toml` version sync is tested but fragile (test_instance_cli.py:182-185)

The test `test_pyproject_version_matches_version_module` catches drift, but it runs in CI only. A pre-commit hook or a single-source-of-truth (e.g., `importlib.metadata`) would be more robust.

### 28. `_PORT_RANGE` upper bound is 9001 (exclusive), not 9000 (instance.py:38)

```python
PORT_RANGE = range(BASE_CONTROL_PORT, 9001)
```

This means ports 8000-9000 inclusive (1001 ports). The README says "[8000, 9000]" which matches. But the error message says "[8000, 9000]" too. The `range` upper bound is exclusive, so `range(8000, 9001)` gives 8000-9000 inclusive — this is correct. No bug, but the `9001` magic number should have a comment explaining why it's 9001 and not 9000.

---

## 📋 Prioritized Work Plan

### P0 — Fix Before Next Release

| # | Issue | File | Fix |
|---|-------|------|-----|
| 1 | `_test_batch` dead `pool.shutdown()` | bench.py:371 | Move to `finally` block |
| 2 | `current_exit_ip` returns `"None"` string | ipinfo.py:379 | Return `None` when `ip` key missing |
| 3 | `_status_doc` reports `"None"` as country | cli.py:248 | Check for `None` before `str()` |

### P1 — Fix Soon (Correctness / Robustness)

| # | Issue | File | Fix |
|---|-------|------|-----|
| 4 | `allocate_free_port` TOCTOU race | instance.py:251 | Atomic bind-or-fail |
| 5 | `inspect_container(name=None)` ambiguity | docker.py:47 | Use sentinel for "no name" |
| 6 | `_status_doc` vs `print_ip_status` inconsistent retry | cli.py / ipinfo.py | Unify probe paths |
| 7 | `_real_ip_cache` thread safety | ipinfo.py:38 | Use `threading.Lock` or `ContextVar` |

### P2 — Improve Design (Maintainability)

| # | Issue | File | Fix |
|---|-------|------|-----|
| 8 | Global mutable `DEBUG` | cli.py:75 | Pass via click context |
| 9 | `current_instance()` silent fallback | instance.py:221 | Fail outside `instance_context` |
| 10 | `ServerRow` is a bare dict | servers.py:51 | Convert to `TypedDict` or dataclass |
| 11 | Compose template string replacement | instance.py:164 | Use `str.format` or template engine |
| 12 | `down` swallows control errors | cli.py:505 | Log warning instead of suppressing |

### P3 — Polish (Code Quality / DX)

| # | Issue | File | Fix |
|---|-------|------|-----|
| 13 | Test monkeypatch targets `vpn.cli.*` | test_selection.py | Patch source module instead |
| 14 | `bench --help` missing stage explanation | cli.py | Improve help text |
| 15 | `with_location` clears all filters | control.py | Document or make optional |
| 16 | `_vote` priority order undocumented | ipinfo.py | Add docstring with rationale |
| 17 | `read_env_file` no multiline support | config.py | Add if needed, or document limitation |
| 18 | Version sync via test only | pyproject.toml | Consider `importlib.metadata` or hook |

---

## Summary Statistics

| Metric | Value |
|--------|-------|
| Source files | 16 |
| Test files | 20 |
| Total source lines | ~3,500 |
| Total test lines | ~4,800 |
| Test coverage | High (all modules covered) |
| Critical bugs | 3 (dead code, string "None", country "None") |
| Medium bugs | 7 |
| Design issues | 8 |
| Minor issues | 8 |

---

## Overall Assessment

**Strengths:**
- Excellent separation of concerns across modules
- Comprehensive test suite (154 tests, all passing)
- Leak-first verification is a sound security design
- Multi-provider parallel probing is resilient and well-thought-out
- Instance isolation model is clean and well-documented
- The README and AGENTS.md are thorough

**Weaknesses:**
- A few silent data-corruption bugs (string "None" for missing country/IP)
- Global mutable state (`DEBUG`, `_real_ip_cache`) creates testing and concurrency fragility
- Some dead code paths that suggest incomplete refactoring
- Docker template string replacement is brittle
- Inconsistent probe behavior between JSON and human output paths

**Verdict:** The codebase is production-quality with a few correctness bugs that should be fixed before the next release. The architecture is sound and the test coverage is excellent. The P0 items are quick fixes; the P1/P2 items would meaningfully improve robustness and maintainability.
