# Integrated Code Review — Gluetun VPN Manager CLI (`vpn`)

**Reviewers (contributing models):** Big Pickle · Ling 3.0 Flash Fin Free · MiMo V2.5 Free · Muse Spark 1.3 Free
**Date:** 2026-09-19
**Commit reviewed:** `4532c2b` ("Document control-port resolution, bench winner no-op, and recreate verification")
**Version reviewed:** 0.2.6 (`src/vpn/version.py` == `pyproject.toml` == `vpn --version`)
**Repo:** `/workspace`

> This is a **merged** review. Every item lists which reviews contributed it; items reported by more than one review appear **once**, with all contributors noted.

---

## How this review was merged

| Source review | Tests reported | Static analysis | Docker live-run |
|---|---|---|---|
| Big Pickle | 347 passed | ruff + mypy clean, 70 % coverage | No (no daemon) |
| Ling 3.0 Flash Fin Free | 222 passed | ruff + mypy clean | No |
| MiMo V2.5 Free | 154 passed | — | No |
| Muse Spark 1.3 Free | 251 passed | ruff + mypy clean (strict) | No |

> ⚠️ **Meta-observation:** the four reviews report **four different passing-test counts** (347 / 222 / 154 / 251) for the *same* commit. Either the reviews were produced against different environments (interpreter version, `importorskip` behavior, collection filters) or at least one count is inaccurate. This discrepancy itself is worth resolving before trusting any single number — see Test gaps.

---

## Executive summary

All four reviews agree the codebase is well-architected and carefully written: clean module layering, a disciplined per-instance model, fail-closed API-key gating on the core commands, resilient multi-provider IP probing, drift detection, and a strong test suite with clean `ruff`/`mypy`. None found catastrophic breakage; several call the code "production-usable" at the default single-instance workflow.

The merged findings cluster into four themes:

1. **A real bug in the flagship multi-instance feature** — `vpn logs` passes a *container* name to a command that takes *service* names ([C1], Big Pickle + Muse Spark).
2. **Fail-open / false-positive verification edges** — leak checks that report success when the host bare IP is unknown or the observed IP is empty ([C2], Muse Spark + Ling), and literal `"None"` strings leaking into `current_exit_ip()`/`status --json` output ([C3], MiMo + Muse Spark).
3. **Inconsistency contracts** — `ls --json` vs `status --json` disagree on `control_server.enabled` ([M16], Big Pickle + Muse Spark); README says `down`/`dns`/`update` are read-only but they mutate ([M3], Big Pickle); human-vs-`--json` exit-code behavior diverges ([M9], Ling + Muse Spark).
4. **Robustness/design debt** — TOCTOU port allocation ([M7], **Big Pickle + MiMo + Muse Spark**), `.env` inline-comment parsing ([M4], **Big Pickle + Ling + Muse Spark**), non-atomic registry/cache writes ([M8], Big Pickle + Muse Spark), plaintext-HTTP API-key override ([M2], Big Pickle + Ling), bench executor leak ([C4], MiMo + Muse Spark), and a `cli.py` monolith ([M12], Big Pickle).

**Bottom line:** fix the P0 group first (all are small), then the P1 "consistency and trust" pass, then treat the P2/P3 backlog. A consolidated workplan is at the end.

---

## Strengths (reported by all four)

- **Clean layering & separation of concerns** — `instance` → `docker` → `control` → `apply` → `cli`, single responsibilities, few import tangles. [Big Pickle, MiMo, Muse Spark]
- **The per-instance model** — registry + generated per-instance compose file + exact-name matching; a sound answer to the multi-instance problem, well documented in README §Instances. [Big Pickle, MiMo, Muse Spark]
- **Fail-closed API-key gate** on `up`/`connect`/`bench` with a helpful message. [Big Pickle]
- **Resilient public-IP probing** — parallel probes across four echo services, plurality voting with provider-priority tie-break, canonical country names, graceful rate-limit degradation. [Big Pickle, MiMo ("multi-provider parallel probing is resilient and well-thought-out")]
- **Leak checking done right** (in intent) — bare-IP exclusion (see [C2] for the edge), and leak vs "still on old route" distinguished. [Big Pickle]
- **Winner verification on `bench --connect`**; **drift detection** in `status --json`; **`fcntl`-based per-instance locking** and `instance_context` scoping. [Big Pickle]
- **Compose-version awareness** (service-name semantics) elsewhere in the codebase — consistent with C1 being a one-off slip rather than a pattern. [Big Pickle]
- **Strong tests & tooling** — strict mypy, clean ruff, typed dataclasses, CI running everything. [Big Pickle, all]
- **Documentation discipline** — exit codes, instance model, verification semantics, consumer API (`dockerstrator`). [Big Pickle, MiMo]
- **Version-sync discipline** (`version.py` ↔ `pyproject.toml` lockstep, `importlib.metadata` deliberately unused). [Big Pickle]

---

## Findings

Severity scale: **P0** = bug/correctness/security (fix first) · **P1** = major correctness/consistency/robustness · **P2** = minor bugs & design debt · **P3** = polish/nits.

### P0 — Bugs & correctness

#### C1. `vpn logs` passes the container name where a compose *service* name is required
**Contributors:** Big Pickle (F1) · Muse Spark (P1-1)

`cli.py:515–522` sends `container` to `docker compose logs`, but compose takes **service** names. The generated compose file always names the service `gluetun` (`container_name:` is the only thing swapped), so:
- default instance `gluetun`: name and service coincide → works;
- any other instance (e.g. `--instance plan-a`): compose looks up `plan-a` among services (`project.GetServices` in compose v5 `logs.go`) → **"no such service" / empty output**.

Breaks `vpn logs` exactly for the flagship multi-instance case. **Fix:** pass the service name (`gluetun`); add a mocked-compose regression test parametrized over non-default instance names; live-verify on a Docker host.

#### C2. Fail-open leak verification when the host bare IP is unknown / observed IP is empty
**Contributors:** Muse Spark (P0-1) · Ling (#3)

- Muse Spark: `bare = real_ip()` returns `None` offline → the exclude-filter is skipped and **any** container IP yields `ok=True` / `print_ip_status` returns `True`. The README claims degradation to country heuristics, but nothing gates on `matched` in that path — a leaked connection whose exit equals the (unknown) bare IP is reported green.
- Ling: `print_ip_status` returns `True` when `vpn_ip` is the empty string — `"" != bare` evaluates truthy → reports a verified connection when no IP was observed.

**Fix:** fail-closed/degraded mode — warn loudly when the bare IP is unknown, require a country match, and return a distinct `leak=unknown`; guard `bool(vpn_ip)` before comparing.

#### C3. Literal `"None"` strings from missing `ip` / `country` fields
**Contributors:** MiMo (#4, #7) · Muse Spark (P2, ipinfo details)

- `current_exit_ip` (`ipinfo.py:375–379`): `str(info.get("ip"))` → returns the string `"None"` when the `ip` key is missing.
- `_status_doc` (`cli.py:245–249`): `str(info.get("country") or "")` → `resolve_country("None")` passes `"None"` through → `status --json` reports `"country": "None"` instead of `null`.

**Fix:** return `None` when the key is missing (guard before `str()`); add a regression test.

#### C4. Bench `_test_batch` executor leak; `pool.shutdown()` is unreachable
**Contributors:** MiMo (#1) · Muse Spark (P0-3)

`bench.py:356–371`: the `try` **returns** on the success path and **re-raises** on `KeyboardInterrupt`, so `pool.shutdown()` at the tail never executes — threads are cleaned up only by GC/`__del__`. Muse Spark adds:
- the "aborts promptly" claim is false: after `shutdown(wait=False, cancel_futures=True)` the code loops `future.exception()`, which **blocks on running futures**;
- `max_workers=len(names)` raises `ValueError` on empty candidate lists — guard `if not candidates: return []`.

**Fix:** `with ThreadPoolExecutor(...) as pool:` (or a `finally: pool.shutdown(wait=False)`), non-blocking abort, and an empty-input guard.

*Related (Ling #2):* `_test_one`'s `finally: remove_container(name)` also runs when `launch_container` already failed — harmless (`docker rm -f` is idempotent) but a wasted Docker call.

#### C5. Bench parallel workers lose `--instance` context (`ContextVar` doesn't cross threads)
**Contributors:** Muse Spark (P0-2)

`instance.py:218–224` re-resolves from `GLUETUN_INSTANCE`/`.env` per call, and the `instance_context` `ContextVar` does **not** propagate into `ThreadPoolExecutor` workers (`bench.py:347–360`). Parallel bench (`-c > 1`) with `--instance foo` and no exported env raises `UsageError` inside every worker, surfaced as a generic "test failed". **Fix:** pass the resolved `Instance`/env dict explicitly into `_test_one`/`_test_batch`; memoize the resolved instance per command.

#### C6. `docker.inspect_container`'s `except OSError` is dead — read-only probes exit the process
**Contributors:** Muse Spark (P0-4)

`docker.py:35–40` converts `OSError` to `SystemExit(127)`, so the `except OSError: return None` at `docker.py:61–62` never fires. `container_status`/`container_running`/`container_env` therefore **exit the process** instead of returning `None` when Docker is unavailable. **Fix:** catch `SystemExit` there or make `run()` not exit on `check=False` paths.

#### C7. `control._request` lets non-HTTP errors escape as raw tracebacks
**Contributors:** Muse Spark (P0-5)

`control.py:47–70` catches only `TimeoutError`/`HTTPError`/`URLError`; `ValueError` (malformed address), `socket.gaierror`/`OSError` variants escape, and callers (`cli.py:190–195`) catch only `ControlError` → raw traceback. **Fix:** catch `(URLError, OSError, ValueError)` → `ControlError`.

#### C8. Probed-exit-IP paths diverge: `status --json` conflates probe failure with a leak, and JSON vs human probe counts differ
**Contributors:** Muse Spark (P0-6) · MiMo (#9)

- Muse Spark: `_probe() is None` sets `leak=True` with no `last_error` — a transient docker-exec/DNS failure exits `status --json` with code 1 *as if leaking*. Distinguish `leak: bool` from probe health / populate `last_error`.
- MiMo: `_status_doc` (JSON) does a single `_probe()` while `print_ip_status` (human) retries up to 15× — JSON can report `leak: true` while the human path eventually succeeds.

**Fix:** one shared probe/verification helper used by both output paths, with a clear probe-health signal.

#### C9. Secrets passed in `docker run -e` argv for bench containers
**Contributors:** Muse Spark (P0-7)

`docker.py:131–149` (`launch_container`) puts `-e KEY=val` on the argv — visible via `ps`/`docker inspect` — while the compose path correctly passes env. Bench temp containers take the leaky path. **Fix:** `--env-file` with `0o600` (or env-passing) for `docker run`.

---

### P1 — Major

#### M1. `ls --json` and `status --json` disagree on `control_server.enabled`
**Contributors:** Big Pickle (F2) · Muse Spark (P2)

- `status --json` (`cli.py:232,260`): `enabled` = the control server actually responded with a valid selection.
- `ls --json` (`discovery.py:121–123`): `enabled = state == "running"` — the container merely exists.

A running but unreachable instance reports `enabled: true` in `ls --json` and `enabled: false` in `status --json`. **Fix:** unify on the control probe; share one schema builder (`reachable` + tunnel state, per Muse Spark) so hand-maintained schemas can't drift.

#### M2. API key can be sent over plaintext HTTP to an off-loopback host
**Contributors:** Big Pickle (F6) · Ling (#6)

`control.py:36–44`: `HTTP_CONTROL_SERVER_ADDRESS` (documented as user-overridable) is used as-is. Set to `http://some-host:8000`, the `X-API-Key` header (`control.py:58`) travels in cleartext, with no loopback validation and no warning. The default (`vpn.yml` pins `127.0.0.1`) is safe. **Fix:** warn/refuse non-loopback `http://`; optionally support `https://`; Ling suggests restricting to `http://127.0.0.1:` / `http://localhost:` or removing the escape hatch.

#### M3. README/behavior mismatch: `down`, `dns on/off`, `update` mutate but are documented as read-only and skip the API key
**Contributors:** Big Pickle (F4)

`README.md:252` calls `status`/`logs`/`ls`/`dns`/`update`/`down` read-only, and claims only `up`/`connect`/`bench` mutate. But `down` (`cli.py:500–508`), `dns` (`735–750`), and `update` (`753–762`) all mutate runtime state *without* `require_api_key` (only `up`/`connect`/`bench` call it, at `380,469,677`). Either the README is wrong or the omission is deliberate — make it a documented decision, not an accident. (`dns off` reconfigures DNS/firewall; an API key is required to *start* the VPN but not to *stop* it.) *Related:* Ling (#14) suggests a `@require_api_key` decorator to make gating uniform and declarative.

#### M4. `read_env_file` parsing gaps — inline comments, quote handling, multiline, key validation
**Contributors:** Big Pickle (F12) · Ling (#1) · MiMo (#12, #26) · Muse Spark (P1-9)

`config.py:87–99`: lines starting with `#` are skipped, but **inline `#` comments are kept in the value** (`KEY=value # comment` → `"value # comment"`), which can corrupt credential values. Reported independently by **three** reviews. Further gaps:
- MiMo: quote-matching edge (`'"hello"'` strips outer quotes, leaves inner `'hello'`); multiline values unsupported.
- Muse Spark: accepts `=value` (empty key) with no key validation; TOCTOU/unhandled `OSError`; `KEY= # comment` keeps the comment.

**Fix:** strip inline comments (preserving `#` inside quoted values), reject empty keys, handle/at minimum document the supported subset (Ling suggests splitting the value on `#` after extraction); add tests for inline comments, quoted `#`, `KEY=`, quotes, unicode.

> Big Pickle explicitly confirmed CRLF (`\r\n`) is handled fine by the existing `.strip()`s — no action needed there.

#### M5. `CACHE_TTL` bound at import; bad input crashes every invocation
**Contributors:** Big Pickle (F14) · Muse Spark (P1-9)

`config.py:20`: `int(os.getenv("GLUETUN_CACHE_TTL", "3600"))` runs at import — (a) a changed `.env` value is ignored for the process lifetime (Big Pickle), and (b) a non-integer value crashes **every** invocation including `--help` (Muse Spark). **Fix:** read per call/command with a fallback-tolerant parse.

#### M6. Registry-less instance falls back to probing `127.0.0.1:8000` (foreign-instance misattribution risk)
**Contributors:** Big Pickle (F3)

`instance.py:202–203`: when an instance has no registry record and no published port, every command targets `127.0.0.1:8000`. If a different instance/unrelated service holds that port and speaks the control API, the CLI reads/writes the wrong instance's selection. The fallback is unverifiable. **Fix:** verify identity (match container `Names`/`Id` against what the control server reports) or at minimum warn loudly when falling back.

#### M7. TOCTOU race in `allocate_free_port`
**Contributors:** Big Pickle (F13) · MiMo (#3) · Muse Spark (P1-9)

`instance.py:242–256`: check-then-bind is not atomic — two concurrent `vpn up` runs can pick the same port; the second `compose up` then fails on the published-port bind (or binds a port different from the registry claim). Muse Spark adds: docker-published-but-unbound ports are ignored, and there's no registry-reservation check. **Fix:** bind-and-hold the socket until just before compose, or re-bind-check immediately before writing the registry.

#### M8. Non-atomic registry/cache writes + silent corrupt-JSON handling
**Contributors:** Big Pickle (F10) · Muse Spark (P1-9)

- Big Pickle: `servers.py:175–177` writes `servers.json` without tmp+rename — a crash mid-write corrupts the cache and the 404→refresh hint may misfire.
- Muse Spark: `write_registry` (`instance.py:95–122`) is likewise non-atomic, and `read_registry` **swallows** corrupt JSON, silently triggering fresh-port allocation + the "registry-less" fallback (which then feeds M6) with no warning.

**Fix:** temp-file + `os.replace` for both, and warn on a corrupt registry.

#### M9. Exit-code / error contracts are inconsistent
**Contributors:** Ling (#8) · Muse Spark (P1-4)

- Ling: `raise SystemExit("message")` (no code) exits with code **0** on error paths, violating the documented "exit 1 on failure" contract; the codebase mixes `SystemExit` / `click.UsageError` / `RuntimeError`(via `ControlError`) styles.
- Muse Spark: human `status` always exits 0 (ignores `finish_connection()`'s return; "not found" also 0) while `status --json` exits 1 on leak; empty-list results succeed silently.

**Fix:** standardize 0 = ok / 1 = verification-or-runtime failure / 2 = usage in *both* modes; always `SystemExit(1)` (or `SystemExit(1, ...)`) on error paths.

#### M10. Global mutable `DEBUG` flag
**Contributors:** Ling (#4) · MiMo (#13)

`cli.py:75,326–327`: a module-level `global DEBUG` makes `_log_env` depend on mutable state; untestable without side effects and not thread-safe. **Fix:** `contextvars.ContextVar` or pass through click's context.

#### M11. Ambient per-call instance resolution (`current_instance()`)
**Contributors:** Ling (#5) · MiMo (#14)

- Ling: `current_instance()` outside an `instance_context` re-runs `resolve_instance → required_name → os.getenv → build_env()` (`.env` read + re-parse) on **every** call — hot in `docker.py`/`control.py`/`ipinfo.py`/`speedtest.py`. Cache/memoize, or rely on scoped context only.
- MiMo: the silent fallback to `default_instance()` makes behavior context-sensitive — code that forgets `instance_context` "works" when `GLUETUN_INSTANCE` is set and fails otherwise. Pick one behavior (always fail outside context, or always resolve) and be consistent.
- Muse Spark's C5 is the concrete breaker this creates: `ContextVar` context is lost in thread pools.

#### M12. `vpn down` robustness: swallowed errors + missing compose file
**Contributors:** MiMo (#10) · Muse Spark (P1-1)

- MiMo: `down` (`cli.py:504–506`) uses `contextlib.suppress(Exception)` — genuine `ControlError` diagnostics ("auth failed") are discarded; the user sees "VPN stopped." even when it wasn't told to stop.
- Muse Spark: `down` never calls `ensure_compose_file(inst)`; `compose("down")` on a never-created instance runs `docker compose -f <missing> down` → errors.

**Fix:** log/report the control error instead of suppressing; ensure the compose file exists before `down` (and degrades gracefully).

#### M13. `with_location` filter/credential handling
**Contributors:** MiMo (#5) · Muse Spark (P1-5)

Two distinct aspects of the same function:
- MiMo: `with_location` clears **all** location filter lists unconditionally (`control.py:182–183` — regions, categories, isps, hostnames, names, numbers). Intentional per the docstring, but lossy: users can't combine `vpn connect` with pre-set gluetun filters. Document or make optional.
- Muse Spark: the `deepcopy` retains opposite-protocol credentials — switching WireGuard→OpenVPN keeps `wireguard.*` env (and vice versa), and stale `WIREGUARD_ADDRESSES` survive when the new env omits them; the server-side PUT merge means unused secrets persist. Clear the inactive protocol section and always overwrite addresses for the active protocol.

#### M14. `vpn ls` / discovery: N+1 fan-out + fragile listing
**Contributors:** Big Pickle (F9) · Muse Spark (P1-7)

- Big Pickle: `instance_records` (`discovery.py:108–127`) issues ~2 inspects + a control-server HTTP call + **another full `docker ps -a`** (`consumers_of`) per instance, on top of a leading `docker ps -a` — ~12+ docker invocations for 5 instances.
- Muse Spark: hoist to one `ps` pass grouped by `NetworkMode`; parallelize `_runtime_selection` with a timeout budget. Listing fragility: `_state` maps `paused`/`created` → `"stopped"`; `_control_port` rejects string ports and maps `True` → port 1; `_runtime_selection` catches only `ControlError` so one bad instance kills the whole `ls`; `print_ls_table` indexes `sel['provider']` directly (`KeyError` on corrupt records); `_known_names` is case-sensitive (ghost `MyVPN` vs `myvpn`).

#### M15. Servers fetch/parse: silent-empty failures, partial-fetch caching, brittle markdown parser
**Contributors:** Muse Spark (P1-8) · Ling (#11)

- Muse Spark: `_fetch_servers → []` vs `_fetch_all_servers → None` — total failure becomes `get_servers → {}` with no error → "Benchmarking 0 locations" / empty picker; `_read_cache` assumes a dict (`AttributeError` on `[]`/`"x"`); `parse_server_selection` unpack-crashes on malformed brackets (`"[foo"`); `sorted_server_rows` can `KeyError` and omits protocol from the sort key (nondeterministic); the `sh -c` loop masks per-provider failures (only the last exit code survives) and the fixed 120 s timeout doesn't scale; provider names interpolated into `sh -c` (safe only because they're constants); **partial-fetch `[]` results are cached for the full TTL**, hiding failures — cache only full success or record per-provider errors.
- Ling: `_parse_servers_output` falls back to hardcoded column indices `1, 2` when the header lacks `country`/`city` — fragile to format changes (e.g. a leading `Name` column).

#### M16. Provider re-derivation after stop: registry doesn't persist provider; `up --recreate` ignores baked env
**Contributors:** Big Pickle (F16) · Muse Spark (P1-2)

- Big Pickle: the registry stores only name/port/env_file — `vpn up` on a stopped instance demands `--provider` again even though it was started before.
- Muse Spark: `up --recreate` falls back only to the control-server `current.provider`; when the server is unreachable but `VPN_SERVICE_PROVIDER` is **baked in the container env**, it still demands `--provider` instead of consulting `_baked_selection()`.

**Fix:** persist provider/protocol in the registry or reuse the baked env.

#### M17. Compose template string-replacement fragility (`render_compose`)
**Contributors:** Ling (#17) · MiMo (#19)

`render_compose` does `body.replace("container_name: gluetun", f"container_name: {name}")` — break silently if the template evolves (e.g. a comment containing the same text), and no assertion that substitution happened. Ling adds: the template hardcodes `127.0.0.1:8000:8000/tcp` and `HTTP_CONTROL_SERVER_ADDRESS=:8000` (the latter is *not* replaced by `render_compose`; correctness currently depends on runtime `container_control_port()` discovery). **Fix:** a template placeholder (`{{PORT}}`/`{{NAME}}`) with an assert, or YAML manipulation; update `render_compose` to also handle `HTTP_CONTROL_SERVER_ADDRESS=:8000`.

#### M18. `swap_lock` robustness + long-held lock
**Contributors:** MiMo (#15) · Muse Spark (P1-6)

- Muse Spark: `os.open` without `O_CLOEXEC` leaks the fd into `docker` children; `flock(LOCK_EX)` blocks **indefinitely** (a hung bench can deadlock a later `connect` silently); lock path is duplicated (`apply.py:69` vs `Instance.lock_file` at `instance.py:77–79`) — single source of truth.
- MiMo: `apply_location` holds the lock across the whole GET→mutate→PUT round-trip (PUT alone up to 60 s), so a hung control server blocks the terminal with no feedback.

**Fix:** `O_CLOEXEC` + `LOCK_EX|LOCK_NB` with bounded retry and a "waiting for lock held by pid…" message; reuse one lock path.

#### M19. Provider validation / case gaps
**Contributors:** Muse Spark (P1-10)

`choose_protocol` (`providers.py:72–80`): `if current in active` compares raw case (`current="WireGuard"` misses); returns unvalidated `requested.lower()` — safety depends on callers remembering `validate_provider` (only `resolve_provider` does); `get_protocols`/`get_provider_env` raise bare `KeyError` instead of friendly errors; Surfshark WG injects `WIREGUARD_ADDRESSES` whenever set though only the key is required (scope `env_map` to required vars). **Fix:** casefold protocol comparison; validate in `choose_protocol` itself; friendly errors; scope optional WG vars to the required key.

#### M20. `connect --list` ignores `--instance`; `ls --instance` skips validation
**Contributors:** Muse Spark (P1-3)

- `cli.py:461–466`: `connect --list` returns early before `_resolve_for_command` — the listing is unscoped. Scope it or reject the combination.
- `cli.py:721–725`: `ls` filters on the raw string without `parse_instance_name` validation, unlike every other command.

#### M21. Bench winner-apply failure strands the tunnel
**Contributors:** Muse Spark (P2)

`bench.py:279–290`: when applying the winner fails, bench appends "(restore/connect failed)", exits **0**, and never attempts `restore_settings(baseline)`. **Fix:** attempt restore + non-zero exit.

---

### P2 — Minor bugs & design debt

- **P2-1. Port-adoption logic duplicated** between `_resolve_for_command` and `up` (`cli.py:142–167,367–377`) — a drift hazard (a C1/M6-style fix would need to land twice). Extract `_adopt_or_allocate_port(inst, ctl_port, env_port)`. [Big Pickle F7]
- **P2-2. `cli.py` is drifting toward a monolith** (762 lines; 57 % coverage is the lowest in the command layer). Extract `_status_doc` + schemas → `statusdoc.py` (shared with `ls --json`, fixing M1), Ctrl-C handling → decorator, selection helpers → `apply.py`. [Big Pickle F8]
- **P2-3. `logs -n/--tail` unvalidated** — free-form string forwarded to `docker logs --tail`; `abc` yields a confusing daemon error. Use `click.IntRange(min=0)` or an `all`-accepting type. [Big Pickle F11 · Muse Spark P1-1]
- **P2-4. Ctrl-C handling exists only in `bench`** (clean `exit 130` + settings restore); `up`/`connect`/`status` print raw tracebacks. Add a shared `KeyboardInterrupt → SystemExit(130) from None` wrapper at `main()`. [Big Pickle F5]
- **P2-5. `--provider ""` silently ignored** — falsy empty string falls into the "current protocol" path; reject via a custom click type. [Big Pickle F20]
- **P2-6. `Selection.country` `""` vs `None`** — `_baked_selection` yields `""`, `Selection.from_doc` yields `None`; only masked by the fold in `.key`. Normalize on one sentinel. [Big Pickle F21]
- **P2-7. Worst-case IP-probe latency** — `_probe` up to ~20 s; `fetch_ip_info` 15 retries → ~5.5 min worst case. Lower exec timeout / exponential backoff / early-exit when all providers fail fast. [Big Pickle F22]
- **P2-8. Hostname-less bench candidates have `latency=None`** — ranking must be None-tolerant; use a sentinel ranking key at point of use. [Big Pickle F23]
- **P2-9. No way to forget an instance** — `vpn down` leaves an `absent` ghost row forever; add `vpn rm` / `down --rm`. [Big Pickle F15]
- **P2-10. No `bench --json`; no cache-refresh flag** — `BenchReport` is rich but only human-rendered; server cache has no `--refresh`. [Big Pickle F17, F18]
- **P2-11. Only two hardcoded providers** (`surfshark`, `protonvpn`) — addable but a code change each; a config-driven extension point would help. [Big Pickle F19]
- **P2-12. `up`'s `requested` override logic is confusing** — after container creation `requested` is recomputed from country/city only; a reader would expect `--provider` to trigger hot-swap. Rename (`location_requested`) or extract a helper. [Ling #7]
- **P2-13. `_vote` plurality tie-breaking is implicit** (provider priority order undocumented) — works, but non-obvious when a user sees an unexpected IP; add a docstring with rationale. [Ling #10 · MiMo #16]
- **P2-14. Module-level `_container_ids = itertools.count()` never reset** — fine in one-shot CLI processes, unbounded in long-running/server contexts. [Ling #12 · MiMo #25]
- **P2-15. Docker layer implicitly resolves `current_instance()` via `name=None`** — `inspect_container`/`container_x(name=...)` default to the current instance, so an explicit `None` is ambiguous (sentinel `Ellipsis` would disambiguate); `compose()` also always uses `current_instance()` (consider an `inst` parameter). [Ling #13 · MiMo #2, #21]
- **P2-16. `compose()` env merge semantics unclear** — three-layer merge (`env_overrides` → `.env` → process env) is correct but the comment doesn't say so; document explicitly. [MiMo #8]
- **P2-17. `_real_ip_cache`/`_real_ip_info` process globals** — mutated from module level; safe today (main-thread only) but fragile in threaded contexts; use a lock or `ContextVar`. [MiMo #11]
- **P2-18. `fetch_ip_info` always excludes the bare IP** — correct for leak detection but a hidden invariant that can surprise future callers (e.g. a diagnostic command). [MiMo #6]
- **P2-19. `connect` probes `prev_ip` before showing "Swapping…"** — a slow `docker exec` delays feedback; move the message/probe ordering. [MiMo #17]
- **P2-20. Plain-dict typing** — `ServerRow` is `dict[str, str]`; `format_result` indexes an unvalidated dict. Convert to `TypedDict`/dataclass for type safety. [MiMo #18, #23 · Muse Spark P2 (speedtest)]
- **P2-21. Bench CLI help unclear** — `--top`/`--scan-size`/`--size` don't explain the 3-stage pipeline (latency → screening → finals); `--max-candidates 0` meaning "no cap" should be `None`-typed. [MiMo #20 · Muse Spark P2]
- **P2-22. `_parallel_stage` progress display** — `[start+1–start+len(batch)]` is 1-indexed-correct but looks off; `min(start+concurrency, len(pool))` reads better. [MiMo #22]
- **P2-23. Test monkeypatches target `vpn.cli.*` instead of the source module** — silently breaks under a refactor to `from vpn import ipinfo`; patch `vpn.ipinfo.print_ip_status` instead. [MiMo #24]
- **P2-24. Version sync enforced only by a CI test** — single-source (e.g. `importlib.metadata`) or a pre-commit hook; see P3-27 for the wider packaging item. [MiMo #27 · Muse Spark P2]
- **P2-25. `PORT_RANGE` handling** — `range(8000, 9001)` upper bound is a magic number (actually correct: 8000–9000 inclusive); but `GLUETUN_CTL_PORT` env and registry/discovery *string* ports are unvalidated/uncoerced versus the CLI flag's `IntRange(1,65535)`. Validate + coerce centrally. [MiMo #28 · Muse Spark P2]
- **P2-26. DRY/API-contract niceties** — `@require_api_key` decorator (uniform with M3); missing `__all__` in most modules despite a documented consumer API (`dockerstrator`). [Ling #14, #15]
- **P2-27. Low-risk polish** — externalize `countries.py` data (Ling #18); simplify picker `class:`-prefixed styles (Ling #19); refactor `run_bench`'s `nonlocal`-heavy control flow into `_run_serial_stage`/`_run_parallel_stage`/`_determine_winner`/`_apply_winner` (Ling #20).

---

### P3 — Nits & polish (single-review, mostly Muse Spark unless noted)

- **P3-1. Baked multi-value truncation** — `SERVER_COUNTRIES="NL,DE"` stored verbatim breaks drift compare and display while `Selection.from_doc` keeps only `[0]` → false-negative drift. Split/take `[0]` consistently or preserve lists. [Muse Spark]
- **P3-2. No country/city pre-validation** — typos (`--country Netherlandz`) surface only after a PUT + slow IP probe; cheap `get_servers()` check fails fast. Protocol-switch keeping location can also strand on cities with no servers for the new protocol. [Muse Spark]
- **P3-3. Bench env divergence** — `_BENCH_ENV_TUNING` hardcodes MTU/DOT/DNS/firewall instead of copying live container values; `SERVER_CITIES=""` vs runtime `[]` semantics may differ; uncredentialed candidates burn full launch+verify cycles (filter via `get_active_providers()`/`listable_servers` first). [Muse Spark]
- **P3-4. `ipinfo` details** — `VPN_REAL_IP` honored by `real_ip()` but not `real_ip_info()` (bare row missing vs verdict using override); no `ipaddress` validation (whitespace/IPv6 false red/green); keyless `ip2location` endpoint always errors (wasted 4th probe thread); `_vote()` empty-input `KeyError` risk; `_probe` unguarded `future.result()` and `None`-stdout crash; `_fmt_row` unused `label` and non-ASCII `▸`; `fetch_ip_status` first/last-attempt silence asymmetry; `bare = real_ip()` per call = host I/O per attempt. [Muse Spark]
- **P3-5. `latency.probe_host` catches only `OSError`** — `UnicodeError`/`ValueError` kills the whole `pool.map` batch; use per-future guards. Keep the documented TCP-443 caveat. [Muse Spark]
- **P3-6. `countries` aliases** — only 21; missing `uk→GB`, `usa→US`, `uae`, `england`, alpha-3 (`USA`/`GBR`/`DEU`→`None`); `--country USA` misses. Add alpha-3 map + `casefold`. [Muse Spark]
- **P3-7. `textutil`** — `fold` uses `.lower()` not `.casefold()`; `strip_accents` deletes `ß/ł/æ/œ/đ` (no decomposition) instead of transliterating; no tests for any of the three functions. [Muse Spark]
- **P3-8. `speedtest`** — `mbps()` has no zero-guard (safe only via `measure` clamp); `format_result` unvalidated dict indexing; no `size_mb > 0` check outside the CLI; missing `capture=True` (wget errors stream raw); assumes `wget` + GNU `timeout` in the image (no curl fallback). [Muse Spark]
- **P3-9. `picker`** — empty `event.data` resets cursor (`"".isprintable()` is `True`); query stores folded text so backspace desyncs on non-ASCII; `max()` over empty `rows` crashes on direct construction; column switch forgets per-column value; no `Delete`/`C-u`/`C-w`/no-match hint; non-TTY `run()` traceback not converted to a friendly error. [Muse Spark]
- **P3-10. Env merge order & CWD coupling** — `{**os.environ, **base}` lets the default `.env` override exported vars (inverted 12-factor surprise), and `compose()` then shadows fresh process env with a stale snapshot — store `base` separately and merge fresh; explicit `--env-file` typo silently falls back to `{}` (error when the explicit path is missing); the default `.env` is CWD-dependent. [Muse Spark]
- **P3-11. Messaging/nits** — `ControlError.__str__` already prefixes "control server:" so the caller's `Cannot reach control server: {exc}` double-prefixes; human `status` swallows `ControlError` with bare `pass` while `dns`/`update`/`bench` abort (standardize on `exc.message`); `finish_connection(expected_country=target.country or None)` vs `=target.country` `""`-vs-`None` fragility; `timeout: int` annotations but floats used; `put_settings` return discarded; `container_control_port` crashes on JSON `null` ports (`isinstance` guard); `{timeout:g}` crashes on `None`; `launch_container` drops stderr (returns ok + tail); `container_env()` `{}` ambiguous absent-vs-empty; `render_compose` never asserts substitution happened (ties into M17). [Muse Spark]
- **P3-12. Packaging/docs hygiene** — version duplicated (`version.py` + `pyproject.toml`) with enforcement only via test (see P2-24); entry point `vpn = "vpn:main"` couples packaging to `__init__` re-export (prefer `vpn.cli:main`); unpinned `click/prompt-toolkit/rich` + `:latest` gluetun tag (non-reproducible); `[tool.mypy] files=` is not a valid config key (dead); ruff lacks `S` (bandit); `vpn.yml:19` fail-open empty api-key default; `DOT=off` + plaintext `1.1.1.1` undocumented privacy tradeoff; `.env.sample` `changeme` placeholders pass truthiness checks (generate + refuse); README `vpn 0.2.6` literal will rot; `GLUETUN_CTL_PORT`/bakeable `SERVER_*` sample entries missing. [Big Pickle F24 (unpinned deps + `tomllib` skip on 3.10) · Muse Spark]
- **P3-13. JSON schemas documented only in code docstrings** — `status --json`/`ls --json` contracts (and the README exit-code table, which is prose) deserve a stable documented contract; M1 shows the danger of hand-maintained schemas. [Big Pickle F25]

---

## Security notes (consolidated)

1. **API key over plaintext HTTP (M2)** — default is loopback-pinned and safe; the override path is the risk. Warn/refuse non-loopback `http://`. [Big Pickle, Ling]
2. **Unauthenticated state mutation (M3)** — `down`, `dns off`, `update` mutate without the API key; `dns off` reconfigures DNS/firewall. Make it a documented decision. [Big Pickle]
3. **Secrets in `docker run` argv (C9)** — bench temp containers expose env via `ps`/`docker inspect`. [Muse Spark]
4. **Stale protocol credentials survive `with_location` (M13)** — unused `wireguard.*`/OpenVPN secrets persist server-side on protocol switches. [Muse Spark]
5. **Fail-open leak verdict (C2)** — a leaked connection can report green when the bare IP is unknown. [Muse Spark, Ling]
6. **Sensitive env masking in `--debug` (`cli.py:95–103`)** — the mask list (key/password/token) must stay in sync with future keys (`OPENVPN_PRIVATE_KEY` variants); a masking regression test would help. [Big Pickle]
7. **Control server binds loopback; compose pins `127.0.0.1`; exact-name docker exec; no secrets written to disk** — all good by default. [Big Pickle]

---

## Testing gaps (consolidated)

| Gap | Related finding | Contributors |
|---|---|---|
| Compose calls use the right identifiers (service vs container); parametrize over non-default instance names | C1 | Big Pickle |
| `ls --json` vs `status --json` schema-agreement property test | M1 | Big Pickle |
| `BASE_CONTROL_PORT` fallback warning/identity check (unit-test the decision once extracted) | M6 | Big Pickle |
| Ctrl-C through the shared wrapper / entrypoint | P2-4 | Big Pickle |
| `read_env_file` edge cases: inline comments, quoted `#`, `KEY=`, quotes, empty keys, unicode | M4 | Big Pickle, Ling, MiMo, Muse Spark |
| Cache corruption path (feed truncated `servers.json`; assert refetch + rewrite) | M8 | Big Pickle |
| `logs -n abc` friendly error | P2-3 | Big Pickle |
| `allocate_free_port` re-bind check before registry write | M7 | Big Pickle |
| `_test_one` launch-failure path (no `remove_container` call) | C4 (Ling #2) | Ling |
| `_status_doc` `state == "starting"` drift path; `_request` `TimeoutError`/`URLError`; `container_env` edge cases | — | Ling, Muse Spark |
| Literal `"None"` cases (`current_exit_ip`, `_status_doc` country) | C3 | MiMo, Muse Spark |
| Version-sync test exists but runs only in CI — promote to pre-commit / single source | P2-24 | MiMo, Muse Spark |
| `textutil` (fold/strip_accents/fold_mapped), `speedtest.format_result` + `mbps(x, 0)`, `render_compose`/`vpn.yml` substitution asserts, `.env.sample` keys vs `providers.PROVIDERS` | M17, P3-7, P3-8 | Muse Spark |
| Missing-instance error path never exercised — `conftest` sets `GLUETUN_INSTANCE=gluetun` globally; add a test deleting the var ("no instance → exit 2") | — | Muse Spark |
| CWD isolation — `conftest.isolated_dirs` redirects cache/registry but not CWD/`.env`; a real repo-root `.env` can leak into tests | P3-10 | Muse Spark |
| Fixture test for real `format-servers` output (parser drift), corrupt-registry warning, empty-`get_servers` error path | M15, M8 | Muse Spark |
| Monkeypatch the source module, not `vpn.cli.*` | P2-23 | MiMo |
| **Resolve the 4-way discrepancy in reported test counts (347/222/154/251) for the same commit** | — | Meta |

---

## Prioritized workplan

### Phase 0 — correctness & security (small, do first)
1. **C1** — fix `logs` to pass the compose *service* name; mocked-compose test; live-verify on Docker. (~0.5 d)
2. **C2** — fail-closed leak verdict when bare IP unknown / empty IP. (~0.25 d)
3. **C3** — kill literal `"None"` in `current_exit_ip` and `_status_doc` country. (~0.1 d)
4. **C4** — `with ThreadPoolExecutor as pool` + empty guard + non-blocking abort. (~0.25 d)
5. **C5** — thread the resolved `Instance`/env explicitly into bench workers. (~0.5 d)
6. **C6** — `inspect_container`/probe paths return `None` instead of exiting. (~0.25 d)
7. **C7** — `control._request` catches `(URLError, OSError, ValueError)` → `ControlError`. (~0.1 d)
8. **C8** — split `leak` from probe health; one shared verification helper. (~0.25 d)
9. **C9** — `--env-file` (0600) for `docker run`. (~0.25 d)

### Phase 1 — consistency & trust
M1 unified `enabled`/schema builder · M2 plaintext-HTTP warning · M3 API-key policy + README · M4 `read_env_file` hardening · M5 `CACHE_TTL` per-call parse · M6 port-8000 fallback identity/warning · M7 TOCTOU port hold · M8 atomic writes + corrupt warning · M9 exit-code unification · M10 `DEBUG` → `ContextVar`. (~3–4 d total)

### Phase 2 — robustness & design debt
M11 instance resolution memoization · M12 `down` fixes · M13 `with_location` creds/filter scoping · M14 one-pass `ls` + parallel selection · M15 servers fetch/parse hardening · M16 provider re-derivation · M17 `render_compose` placeholders · M18 `swap_lock` `O_CLOEXEC` + bounded wait · M19 provider validation · M20 list scoping · M21 bench restore-on-failure · then P2-1/P2-2 (extract `statusdoc.py` + port-adoption helper — **before** the P3 features so they build on shared helpers, per Big Pickle), P2-3…P2-27.

### Phase 3 — polish & nits
P3-1…P3-13 as a backlog (picker/countries/textutil/speedtest, env merge order, messaging, packaging/docs). Also resolve the **test-count discrepancy** flagged at the top before trusting CI numbers.

---

## Appendix — review pedigree

| Finding | Big Pickle | Ling | MiMo | Muse Spark |
|---|---|---|---|---|
| C1 `logs` service-name | F1 | — | — | P1-1 |
| C2 fail-open leak / empty IP | — | #3 | — | P0-1 |
| C3 literal `"None"` | — | — | #4, #7 | P2 (ipinfo) |
| C4 executor leak | — | (#2) | #1 | P0-3 |
| C5 thread context | — | — | — | P0-2 |
| C6 inspect OSError dead | — | — | — | P0-4 |
| C7 `_request` tracebacks | — | — | — | P0-5 |
| C8 status leak/probe split | — | — | #9 | P0-6 |
| C9 secrets in argv | — | — | — | P0-7 |
| M1 `enabled` semantics | F2 | — | — | P2 |
| M2 plaintext HTTP key | F6 | #6 | — | — |
| M3 API-key gate/README | F4 | (#14) | — | — |
| M4 `read_env_file` | F12 | #1 | #12, #26 | P1-9 |
| M5 `CACHE_TTL` | F14 | — | — | P1-9 |
| M6 port-8000 fallback | F3 | — | — | — |
| M7 TOCTOU port | F13 | — | #3 | P1-9 |
| M8 atomic writes | F10 | — | — | P1-9 |
| M9 exit codes | — | #8 | — | P1-4 |
| M10 global `DEBUG` | — | #4 | #13 | — |
| M11 ambient `current_instance()` | — | #5 | #14 | (P0-2) |
| M12 `down` robustness | — | — | #10 | P1-1 |
| M13 `with_location` | — | — | #5 | P1-5 |
| M14 `ls` N+1 | F9 | — | — | P1-7 |
| M15 servers fetch/parse | — | #11 | — | P1-8 |
| M16 provider re-derivation | F16 | — | — | P1-2 |
| M17 `render_compose` | — | #17 | #19 | (P3-11) |
| M18 `swap_lock` | — | — | #15 | P1-6 |
| M19 provider validation | (F20) | — | — | P1-10 |
| M20 list scoping | — | — | — | P1-3 |
| M21 bench restore-on-fail | — | — | — | P2 |
| P2-3 `--tail` | F11 | — | — | P1-1 |
| P2-4 Ctrl-C | F5 | — | — | — |
| P2-5 `--provider ""` | F20 | — | — | — |
| P2-13 `_vote` | — | #10 | #16 | — |
| P2-14 `_container_ids` | — | #12 | #25 | — |
| P2-15 docker/`name=None` | — | #13 | #2, #21 | — |
| P2-20 dict typing | — | — | #18, #23 | P2 (speedtest) |
| P2-24/25 version+ports | (F24) | — | #27, #28 | P2 |
| P3-12 packaging/docs | F24 | — | — | P2 |
| P3-13 JSON schema docs | F25 | — | — | — |

Everything else in P2/P3 is single-review and is attributed inline.