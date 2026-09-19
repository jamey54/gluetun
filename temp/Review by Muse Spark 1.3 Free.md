# Design & Code Review — vpn (Gluetun CLI)

- **Reviewer:** Muse Spark 1.3 Free (`muse-spark-1.3-contributor-free`)
- **Date (UTC):** 2026-09-19
- **Repo state:** version `0.2.6` (`src/vpn/version.py` + `pyproject.toml` in sync), ~3.8k LOC across 18 modules, 251 tests
- **Verified baseline:** `pytest -q` → 251 passed; `ruff check src tests` → clean; `mypy src` → clean (strict). No TODO/FIXME markers in tree.

## Overall assessment

Well-structured small CLI: clean module split (cli / instance / control / apply / discovery / servers / bench / ipinfo / picker), good README, good test coverage of happy paths. Main risks are structural, not stylistic:

1. Ambient per-call instance resolution (`instance.py:218-224`) re-reads disk on every lookup and is invisible in `ThreadPoolExecutor` workers — breaks parallel bench with `--instance` and no env var.
2. Fail-open leak verification when the host bare IP is unknown (`apply.py:117-149`, `ipinfo.py:372`).
3. Inconsistent error/exit-code contracts (empty-list silent success, human vs `--json` status leak codes, `SystemExit` vs `None`/`[]`/`{}`).
4. Secrets in `docker run -e` argv (`docker.py:131-149`) vs env-passing in compose path.
5. Version duplication with no enforcing test; `:latest` pin; brittle markdown-table server parsing.

Details below. Line refs are to current HEAD.
## P0 — bugs / correctness (fix first)

### P0-1. Fail-open leak check when bare IP is unknown
- `src/vpn/apply.py:117-149` + `src/vpn/ipinfo.py:263-264,372`: `bare = real_ip()` returns `None` offline; exclude-filter is skipped and any container IP yields `ok=True` / `print_ip_status` returns `True`.
- A leaked connection whose exit equals the (unknown) bare IP is reported green. README claims "degrades to country heuristics" but code does not gate on `matched` in that path.
- **Fix:** fail-closed/degraded mode — warn loudly, require country match, or return `leak=unknown` distinctly from `false`.

### P0-2. Bench parallel workers lose `--instance` context
- `src/vpn/instance.py:218-224` re-resolves from `GLUETUN_INSTANCE`/`.env` per call; `ContextVar` scope does not propagate into `ThreadPoolExecutor` workers (`bench.py:347-360` → `_bench_container_env` → `env_lookup` → `current_instance()`).
- Parallel bench (`-c > 1`) with `--instance foo` and no exported env raises `UsageError` inside every worker, surfaced as generic "test failed".
- **Fix:** pass resolved `Instance`/env dict explicitly into `_test_one`/`_test_batch`; memoize resolved instance per command.

### P0-3. Bench `_test_batch` executor leak + blocking abort
- `src/vpn/bench.py:356-371`: `return [future.result() ...]` exits before `pool.shutdown()` — successful batches leak threads until GC. Fix with `with ThreadPoolExecutor(...) as pool:`.
- Same function: after `shutdown(wait=False, cancel_futures=True)` it loops `future.exception()`, which blocks on running futures — "aborts promptly" claim is false. Also `max_workers=len(names)` crashes (`ValueError`) on empty input; guard `if not candidates: return []`.

### P0-4. `docker.inspect_container` dead `except OSError`, exits on probes
- `src/vpn/docker.py:35-40` converts `OSError` to `SystemExit(127)` / returncode 127, so `inspect_container`'s `except OSError: return None` (`docker.py:61-62`) never fires. Read-only probes (`container_status`, `container_running`, `container_env`) exit the process instead of returning `None`.
- **Fix:** catch `SystemExit` there or make `run()` not exit on `check=False` paths.

### P0-5. `control._request` lets non-HTTP errors escape as tracebacks
- `src/vpn/control.py:47-70` catches only `TimeoutError`/`HTTPError`/`URLError`. Malformed address (`ValueError`), `socket.gaierror`/`OSError` variants escape; callers (`cli.py:190-195`) catch only `ControlError` → raw traceback.
- **Fix:** catch `(URLError, OSError, ValueError)` → `ControlError`.

### P0-6. `_status_doc` conflates "probe failed" with "leak"
- `src/vpn/cli.py:240-242`: `_probe() is None` sets `leak=True` with no `last_error`; transient docker-exec/DNS failure exits `status --json` with code 1 as if leaking. Distinguish `leak: bool` from probe health / set `last_error`.

### P0-7. Secrets in `docker run` argv
- `src/vpn/docker.py:131-149` (`launch_container`) puts secrets in argv (`-e KEY=val`), visible via `ps`/`docker inspect`. `compose()` correctly uses process env. Bench temp containers take the leaky path.
- **Fix:** `--env-file` with `0o600` or env-passing for `docker run`.
## P1 — major design / reliability issues

### P1-1. `down` breaks on never-created instances; `logs` uses wrong name class
- `cli.py:501-508`: `down` never calls `ensure_compose_file(inst)`; `compose("down")` (`docker.py:125`) uses `inst.compose_file` which may not exist → `docker compose -f <missing> down` errors.
- `cli.py:517-522`: `logs` passes the *container* name to `docker compose logs` (expects a *service* name). Likely needs `docker logs <container>` or the compose service name. Also `--tail` is an unvalidated string (`default="50"`); use `click.IntRange`/ `type=int`.

### P1-2. `up --recreate` without `--provider` ignores baked env
- `cli.py:404-406`: falls back only to control-server `current.provider`; when the server is unreachable but `VPN_SERVICE_PROVIDER` is baked in, it demands `--provider` instead of consulting `_baked_selection()`. Fall back to baked selection.

### P1-3. `connect --list` ignores `--instance`; `ls --instance` skips validation
- `cli.py:461-466`: early return before `_resolve_for_command` — listing is unscoped. Either scope it or reject the combo.
- `cli.py:721-725`: filters on the raw string without `parse_instance_name` validation unlike every other command.

### P1-4. Human vs `--json` status exit-code contract differs
- `--json` exits 1 on leak (`cli.py:557-558`); human path ignores `finish_connection()`'s return (`cli.py:599-603`), always exits 0. Human "not found" (`cli.py:561-563`) also exits 0. Standardize: 0 ok / 1 verification-or-runtime failure / 2 usage, both modes.

### P1-5. `control.with_location` leaves stale opposite-protocol creds + stale WG addresses
- `control.py:160-196`: `deepcopy` retains `wireguard.*` when switching to OpenVPN and vice versa; when new env lacks `WIREGUARD_ADDRESSES`, the old value survives (`189-190`). PUT merges server-side so unused secrets persist. Clear the inactive section; always overwrite addresses for the active protocol.

### P1-6. `swap_lock` blocks forever, leaks fd, duplicated path
- `apply.py:66-77`: `os.open` without `O_CLOEXEC` leaks fd into `docker` children; `flock(LOCK_EX)` blocks indefinitely (hung bench deadlocks connect silently). Use `O_CLOEXEC` + `LOCK_EX|LOCK_NB` with bounded retry and a "waiting for lock held by pid…" message.
- Lock path duplicated (`apply.py:69` vs `instance.Instance.lock_file` `instance.py:77-79`) — reuse one source of truth.

### P1-7. `discovery` N+1 docker/HTTP fan-out, fragile listing
- `discovery.py:87-127`: `consumers_of` runs `docker ps -a` per instance plus 1–2 inspects + sequential HTTP per running instance. Hoist to one `ps`, group by `NetworkMode`, parallelize `_runtime_selection` with a timeout budget.
- `_state` (`22-31`) maps `paused`/`created` → `"stopped"`; `_control_port` (`58-64`) rejects string ports and maps `True` → port 1; `_runtime_selection` (`67-74`) catches only `ControlError` so one bad instance kills `ls`; `print_ls_table` (`146`) indexes `sel['provider']` directly (`KeyError` on corrupt records); `_known_names` case-sensitivity (`49-55`) can ghost `MyVPN` vs `myvpn`.

### P1-8. `servers.py` silent-empty failures + brittle parser
- `_fetch_servers → []` vs `_fetch_all_servers → None` (`96-155`); total failure becomes `get_servers → {}` with no error → "Benchmarking 0 locations" / empty picker. Raise with a hint when all providers fail.
- `_read_cache` (`158-172`) assumes dict (`AttributeError` on `[]`/`"x"`); `parse_server_selection` (`29-43`) unpack-crashes on malformed brackets (`"[foo"`); `sorted_server_rows` (`219-231`) can `KeyError` and omits protocol from sort key (nondeterministic order).
- Markdown-table parser (`54-93`) requires literal `country`/`city` cells, assumes pipe layout; single-boot `sh -c` loop (`123-141`) masks per-provider failures (only last exit code survives); fixed 120s timeout doesn't scale; provider names interpolated into `sh -c` (safe today only because constants).
- Partial-fetch `[]` cached for full TTL (`199-200`) hides failures — cache only full success or record per-provider errors.

### P1-9. Registry/cache writes non-atomic, silent corruption
- `instance.py:95-122`: `read_registry` swallows corrupt JSON → fresh-port allocation + "registry-less" fallback with no warning; `write_registry` is non-atomic (crash → corrupt → next read `None`). Same class in `servers` cache. Use temp-file + rename; warn on corrupt registry.
- `CACHE_TTL` (`config.py:20`) `int()` at import crashes every invocation (incl. `--help`) on bad input — wrap with fallback. `read_env_file` (`77-100`) has TOCTOU/unhandled `OSError`, accepts `=value` (empty key), keeps `# comments`, no key validation.
- `PORT_RANGE` bind-check-then-use (`instance.py:242-256`) is racy and ignores docker-published-but-unbound ports; concurrent creates can double-allocate (no registry reservation check).

### P1-10. `providers.py` case/validation gaps
- `choose_protocol` (`72-80`): `if current in active` compares raw case; `current="WireGuard"` misses. Returns unvalidated `requested.lower()` — safety depends on callers remembering `validate_provider` (only `resolve_provider` does). `get_protocols` (`50-52`) raises bare `KeyError` vs friendly `SystemExit` elsewhere. `get_provider_env` (`120-127`) same `KeyError` fragility — normalize + friendly error.
- Optional WG addresses leak across providers (`20-29` + `120-127`): Surfshark WG injects `WIREGUARD_ADDRESSES` whenever set though only the key is required — scope `env_map` to required vars.
## P2 — inconsistencies, small bugs, polish

- **Port-range validation gap:** CLI flag uses `IntRange(1,65535)` (`cli.py:129`) but `GLUETUN_CTL_PORT` env (`153-160`), registry `control_port` string form (`instance.py:181-211`), and `discovery._control_port` are unvalidated/uncoerced. Validate + coerce centrally.
- **`enabled` means two things:** `status --json` (`cli.py:232,260`) = reachable-and-provider-set; `ls --json` (`discovery.py:121-123`) = `state == "running"`. Unify (suggest `reachable` + tunnel state).
- **Baked multi-value truncation:** `SERVER_COUNTRIES="NL,DE"` stored verbatim (`cli.py:205-216`) breaks drift compare (`235`) and display; `Selection.from_doc` (`apply.py:41-53`) keeps only `[0]` → false-negative drift. Split/take `[0]` consistently or preserve lists.
- **No country/city pre-validation:** typos (`--country Netherlandz`) found only after PUT + slow IP probe (`cli.py:272-313,451-497`). Cheap check against `get_servers()` fails fast. Protocol-switch keeping location (`293-297`) can also strand on cities with no servers for the new protocol.
- **`bench` winner-apply failure strands tunnel** (`bench.py:279-290`): appends "(restore/connect failed)" suffix, exit 0, no `restore_settings(baseline)` attempt. Must restore + non-zero exit.
- **Bench env divergence:** `_BENCH_ENV_TUNING` hardcodes MTU/DOT/DNS/firewall (`bench.py:47-53`) instead of copying live container values; `SERVER_CITIES=""` (`322-323`) vs runtime `[]` semantics may differ; uncredentialed candidates (`113-134`) burn full launch+verify cycles — filter via `get_active_providers()`/`listable_servers` first.
- **`ipinfo` details:** `VPN_REAL_IP` honored by `real_ip()` but not `real_ip_info()` (`ipinfo.py:62-85`) → Bare row missing vs verdict using override; no `ipaddress` validation (whitespace/IPv6 false red/green); `ip2location` keyless endpoint always errors (wasted 4th probe thread); `_vote()` empty-input `KeyError` risk; `_probe` `future.result()` unguarded (`235`); `_probe_provider` `None`-stdout crash (`187`: use `or ""`); `current_exit_ip` (`375-379`) returns literal `"None"` when `ip` missing; `_fmt_row` unused `label`, non-ASCII `▸`; UI mixed into probing; `fetch_ip_status` first/last-attempt silence asymmetry; `bare = real_ip()` per call = host I/O per attempt.
- **`latency`:** `probe_host` catches only `OSError` (`latency.py:13-23`) — `UnicodeError`/`ValueError` kills whole `pool.map` batch; prefer per-future guards. TCP-443 prescreen limitation should stay documented (443-reachability != VPN latency).
- **`countries` aliases:** only 21 (`countries.py:258-281`); missing `uk→GB`, `usa→US`, `uae`, `england`, alpha-3 (`USA`/`GBR`/`DEU` treated as names → `None`). `--country USA` misses. Consider alpha-3 map + `casefold`.
- **`textutil`:** `fold` uses `.lower()` not `.casefold()`; `strip_accents` deletes `ß/ł/æ/œ/đ` (no decomposition) instead of transliterating; no tests for any of the three functions.
- **`speedtest`:** `mbps()` no zero-guard (safe only via `measure` clamp); `format_result` unvalidated dict indexing (prefer `TypedDict`); no `size_mb > 0` check outside CLI; missing `capture=True` (wget errors stream raw); assumes `wget`+GNU `timeout` in image (no curl fallback).
- **`picker`:** empty `event.data` resets cursor (`picker.py:317-320`, `"".isprintable()` is True — guard `if event.data and ...`); query stores folded text so backspace desyncs on non-ASCII (`320,283-285` — store raw, fold only for match); `max()` over empty `rows` crashes on direct construction (`50-55`); column switch forgets per-column value (`136-148`); no `Delete`/`C-u`/`C-w`/no-match hint; non-TTY `run()` traceback not converted to friendly error at call site.
- **`config/instance` env merge order:** `{**os.environ, **base}` (`instance.py:139`) lets default `.env` override exported vars (inverted 12-factor surprise); `compose()` (`docker.py:125-128`) then shadows fresh process env with stale snapshot — store `base` separately, merge fresh. Explicit `--env-file` typo silently falls back (`read_env_file → {}`); error when explicit path missing. Default `.env` is CWD-dependent — surprising; consider config/instance dir.
- **Messaging/nits:** `ControlError.__str__` already prefixes "control server:" so `Cannot reach control server: {exc}` double-prefixes; human `status` swallows `ControlError` with bare `pass` while `dns`/`update`/`bench` abort — standardize on `exc.message`; `finish_connection(expected_country=target.country or None)` vs `(=target.country)` `""`-vs-`None` fragility; `bench --max-candidates 0` = "no cap" (use `None`); `timeout: int` annotation but floats used; `put_settings` return discarded; `container_control_port` crashes on JSON `null` ports (`docker.py:99-103`, add `isinstance` guard); `{timeout:g}` crashes on `None`; `launch_container` drops stderr (return ok+tail); `container_env()` `{}` ambiguous absent-vs-empty; `render_compose` string-surgery never asserts substitution happened.
- **Packaging/docs:** version duplicated (`version.py` + `pyproject.toml`) with no test; entry point `vpn = "vpn:main"` couples packaging to `__init__` re-export (prefer `vpn.cli:main`); unpinned `click/prompt-toolkit/rich` + `:latest` gluetun tag (non-reproducible, settings-route drift); `[tool.mypy] files=` is not a valid config key (dead); ruff lacks `S` (bandit); `vpn.yml:19` fail-open empty apikey, `DOT=off` + plaintext `1.1.1.1` undocumented privacy tradeoff; `.env.sample` `changeme` placeholders pass truthiness checks (prefer empty + reject `changeme`/bad CIDR; `HTTP_CONTROL_SERVER_API_KEY=changeme` is a public default — generate + refuse); README `vpn 0.2.6` literal will rot; `GLUETUN_CTL_PORT`/bakeable `SERVER_*` sample entries missing.
## Test gaps (all cheap, high value)

1. Version-sync test: `version.__version__ == pyproject[project][version]` + `vpn --version` output.
2. `textutil` (fold/strip_accents/fold_mapped), `speedtest.format_result` + `mbps(x, 0)`, `render_compose`/`vpn.yml` substitution assertions, `.env.sample` keys vs `providers.PROVIDERS`.
3. Missing-instance error path: `conftest` sets `GLUETUN_INSTANCE=gluetun` globally so "no instance → exit 2" is never exercised — add test deleting the var.
4. CWD isolation: `conftest.isolated_dirs` redirects cache/registry but not CWD/`.env`; a real repo-root `.env` can leak into tests — monkeypatch CWD or `build_env`.
5. Fixture test for real `format-servers` output (parser drift), corrupt-registry warning, empty-`get_servers` error path.

## Prioritized workplan

| Prio | Work item | Why |
|------|-----------|-----|
| 1 | Fail-closed bare-IP-unknown verdict + `leak` vs probe-health split | Correctness: prevents false-green leaks |
| 2 | Thread resolved `Instance`/env explicitly; memoize per command | Fixes parallel bench; removes per-lookup disk I/O + inconsistency |
| 3 | `with ThreadPoolExecutor as pool`, non-blocking abort, empty guard | Thread leak + hang on Ctrl-C |
| 4 | `control._request` catch `(URLError, OSError, ValueError)`; `docker` probe paths stop exiting; `servers`/`discovery` per-instance guards | No more tracebacks / whole-listing kills on transient faults |
| 5 | Secrets out of `docker run` argv (`--env-file` 0600) | Active secret exposure |
| 6 | Unify exit codes (leak→1 both modes, absent→non-zero) + single `enabled`/`reachable` semantic | Scriptability (dockerstrator contract) |
| 7 | Atomic registry/cache writes + corrupt warning; `CACHE_TTL`/`read_env_file` hardening; validate/coerce ports centrally | Silent state loss + startup crashes |
| 8 | `with_location` clear inactive-protocol creds + overwrite WG addresses | Stale-secret persistence |
| 9 | `swap_lock` `O_CLOEXEC` + non-blocking bounded wait + single lock path | Deadlock + fd leak |
| 10 | Fix `down` (ensure compose file), `logs` (docker logs / service name), `up --recreate` baked fallback, `connect --list` scoping, validate `--tail`/country/city up front | Most-visible CLI papercuts |
| 11 | Server fetch: error on total failure, don't cache partial, per-provider status, parser fixture test, scale timeout | Kills "0 locations" confusion |
| 12 | Bench: restore-on-winner-failure + non-zero exit, filter uncredentialed, copy live tuning, `SERVER_CITIES` omit-when-None, unique container suffix, stderr tail on launch fail | Stranded-tunnel + wasted-cycle fixes |
| 13 | Version-sync test (or single-source), pin image + deps, entry `vpn.cli:main`, remove dead mypy `files`, enable ruff `S`, refuse `changeme` | Reproducibility / supply chain |
| 14 | Picker/country/textutil/speedtest/latency polish + test gaps above | UX + coverage |

## What was *not* done

No code was changed — review only, per request. All findings above are based on reading HEAD plus running `pytest`, `ruff`, and `mypy` (all green); Docker/gluetun live paths were not executed.
