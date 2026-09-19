# Code Review: Gluetun VPN Manager CLI (`vpn`)

**Reviewer:** Big Pickle
**Date:** 2026-09-19
**Commit reviewed:** `4532c2b` ("Document control-port resolution, bench winner no-op, and recreate verification")
**Version reviewed:** 0.2.6 (`src/vpn/version.py` == `pyproject.toml` == `vpn --version`)
**Repo:** `/workspace`

---

## Table of contents

1. [Executive summary](#1-executive-summary)
2. [Scope and method](#2-scope-and-method)
3. [Strengths](#3-strengths)
4. [Findings at a glance](#4-findings-at-a-glance)
5. [Detailed findings](#5-detailed-findings)
6. [Security notes](#6-security-notes)
7. [Testing gaps](#7-testing-gaps)
8. [Prioritized workplan](#8-prioritized-workplan)
9. [Verification appendix](#9-verification-appendix)

---

## 1. Executive summary

The Gluetun VPN manager is a well-architected, carefully documented CLI: clean layering, a disciplined instance model, fail-closed API-key gating, drift detection, and an unusually strong test suite (347 passing, `ruff` and `mypy` clean at strict settings). The codebase reads like the work of someone who already did several review rounds — most sharp edges are caught by tests or handled explicitly.

That said, the review found **one likely functional bug in a core command** (`vpn logs` breaks for any instance whose name isn't `gluetun`), **two behavioral inconsistencies** between `ls --json` and `status --json` and between documented and actual API-key gating, and a cluster of robustness/UX gaps (non-signal-safe Ctrl-C handling, non-atomic cache writes, a TOCTOU port allocation race, worst-case multi-minute IP probes, a plaintext-HTTP API-key path, and a monolith `cli.py` that is drifting). None are catastrophic; all are fixable. Section 8 proposes a prioritized workplan.

**Bottom line:** the tool is production-usable at the default single-instance workflow, but the multi-instance story (the flagship feature) has the one real bug, and a handful of P1 items deserve attention before wider rollout.

---

## 2. Scope and method

- **Read:** all 18 modules in `src/vpn/`, the test suite (`test_*.py`, `conftest.py`), `README.md`, `pyproject.toml`, `.env.sample`, `.github/workflows/ci.yml`, the bundled `vpn.yml` compose template.
- **Ran:**
  - `python -m pytest` → **347 passed** (1.31 s)
  - `ruff check` → clean
  - `mypy` → clean
  - Coverage report (overall **70%**)
- **Live-verified edge behavior** by running small Python fragments against the built package (Section 9).
- **Not live-verified:** any Docker behavior — this environment has no Docker daemon. The compose-related findings were confirmed against the compose v5 (Go) source and by reading the generated compose file; they are flagged accordingly and should be re-verified on a real host.

Coverage per module (statement):

| Module | Coverage |
|---|---|
| `cli.py` | 57 % |
| `bench.py` | 74 % |
| `discovery.py` | 64 % |
| `docker.py` | 51 % |
| `instance.py` | 53 % |
| `ipinfo.py` | 67 % |
| `picker.py` | 65 % |
| `control.py` | 81 % |
| `providers.py` | 96 % |
| `servers.py` | 88 % |
| `speedtest.py` | 78 % |

---

## 3. Strengths

Genuinely good design throughout; worth calling out explicitly:

1. **Clean layering.** `instance` (naming/registry/env/compose) → `docker` (exec wrapper) → `control` (REST client) → `apply` (selection hot-swap) → `cli` (thin glue). Modules have single, testable responsibilities and almost no import tangles.
2. **The instance model.** Registry + generated per-instance compose file + exact-name matching (never "any gluetun container") is a sound answer to the multi-instance problem, and it's well documented in `README.md` §Instances.
3. **Fail-closed API-key gate.** `up`/`connect`/`bench` refuse to run without `HTTP_CONTROL_SERVER_API_KEY`, with a helpful message naming the port.
4. **Resilient public-IP probing.** Parallel probes across the same four echo services gluetun uses, plurality voting, provider-priority tie-break, canonical country names, and a graceful degradation message when ipinfo.io is rate-limited. This is a genuinely good design that solves a real operational pain point.
5. **Leak checking done right.** Excludes the host's *bare* IP (plus stored previous exit IPs on hot-swap), so "still on old route" and "leaking" are distinct failure modes with distinct messages; a leak is a hard failure (`exit 1` in `--json` mode).
6. **Verification of the winner on `bench --connect`** — connecting is only reported after a re-check confirms the new exit IP actually moved.
7. **Drift detection** in `status --json` (runtime selection vs. compose-baked selection) — a thoughtful feature.
8. **Locking discipline.** `fcntl`-based per-instance lock (`swap_lock`) serializes hot-swaps; `instance_context` scopes instance identity so two instances can't be mixed in one process.
9. **Compose-version awareness.** Explicit `GetServices`-style service-name handling would have prevented the `logs` bug below; the codebase otherwise shows it thought hard about compose semantics (e.g., `--env-file`, project pinning via `-p`).
10. **Strong test suite & tooling.** 347 tests, strict `mypy` (no `ignore` soup), `ruff` clean, typed dataclasses, and a CI workflow that runs all of it.
11. **Documentation discipline.** `README.md` covers exit codes, the instance model, verification semantics, and there is a documented consumer API (`dockerstrator`) for programmatic access.
12. **Version-sync discipline.** `version.py` and `pyproject.toml` in lockstep; `importlib.metadata` deliberately unused.

---

## 4. Findings at a glance

| # | Severity | Area | File:line | One-line summary |
|---|---|---|---|---|
| F1 | **P0 (bug, likely)** | `logs` | `cli.py:515–522` | Passes the **container name** to `docker compose logs`, which takes **service names**; the compose service is always `gluetun`, so non-default instances get "no such service"/empty logs. |
| F2 | P1 (inconsistency) | `ls --json` | `discovery.py:121–123` | `control_server.enabled` = `state == "running"`, whereas `status --json` (`cli.py:232,260`) sets `enabled` only when the control server actually responded. Two JSON schemas disagree on the same field. |
| F3 | P1 (bug) | port fallback | `instance.py:202–203` | Registry-less container with no published port falls back to probing `127.0.0.1:8000` (`BASE_CONTROL_PORT`) — may hit a *different* instance's control server and misattribute its selection. |
| F4 | P1 (doc/behavior mismatch) | API-key gate | `cli.py:502–508, 735–762`; `README.md:252` | README claims only `up`/`connect`/`bench` mutate, but `down`, `dns on/off`, and `update` also mutate runtime state — and they do so without the API key. README calls them "read-only". |
| F5 | P1 (polish/UX) | Ctrl-C | `cli.py:344–440, 451–497, 551–604` | Only `bench` catches `KeyboardInterrupt` (clean `exit 130`, settings restore). `up`/`connect`/`status` print raw tracebacks on Ctrl-C. |
| F6 | P1 (security) | control client | `control.py:36–44` | `HTTP_CONTROL_SERVER_ADDRESS` override can send the API key over **plaintext HTTP to an off-loopback host**; no loopback validation or warning. |
| F7 | P2 (design) | `up` port adoption | `cli.py:142–167` vs `367–377` | Port adoption logic (`registry-less running container → published port`) is duplicated between `_resolve_for_command` and `up`; `up` reimplements a slightly different variant. |
| F8 | P2 (design) | `cli.py` monolith | `cli.py` (762 lines) | Command layer keeps growing (status doc, selection helpers, drift, IP probing glue); harder to test (57 % coverage) and to review. |
| F9 | P2 (perf) | `ls` | `discovery.py:108–127`, `34–46`, `87–105` | N+1 docker calls per instance (`docker ps -a` twice + per-instance `container_status`, `container_control_port`, `consumers_of`), plus two identical `docker ps -a` invocations that could be one. |
| F10 | P2 (robustness) | cache write | `servers.py:175–177` | `servers.json` written non-atomically (no tmp+rename); a crash mid-write corrupts the cache and the 404→refresh hint may then misfire. |
| F11 | P2 (robustness) | `logs --tail` | `cli.py:514` | `-n/--tail` is a free-form string passed straight to docker; not validated as an int (or "all"). |
| F12 | P2 (robustness) | `.env` parsing | `config.py:87–99` | `read_env_file` doesn't strip inline `#` comments, doesn't handle `\r\n` line endings; `KEY=` maps to `""` (fine) but `KEY= # comment` keeps the comment. |
| F13 | P2 (race) | port allocation | `instance.py:251–256` | `allocate_free_port` is check-then-use (TOCTOU): two instances starting concurrently can pick the same port. |
| F14 | P2 (config) | `CACHE_TTL` | `config.py:20` | `GLUETUN_CACHE_TTL` read once at import time; a changed value in `.env` is ignored for the process lifetime. |
| F15 | P3 (UX) | no "forget" command | `cli.py` (registry) | `vpn down` leaves the registry record behind; `vpn ls` forever shows an "absent" instance with no way to remove it. |
| F16 | P3 (UX) | registry lacks provider | `instance.py` | Registry stores only name/port/env_file; `vpn up` on a stopped instance still requires `--provider` even though it was started before. |
| F17 | P3 (feature) | no `bench --json` | `bench.py` / `cli.py:658–710` | Bench has rich structured data (`BenchReport`) but only a human-readable report; scripting it is awkward. |
| F18 | P3 (feature) | cache refresh flag | `servers.py` | No `--refresh` to force re-fetch; users must wait out `CACHE_TTL`. |
| F19 | P3 (feature) | providers hardcoded | `providers.py:41–47` | Only `surfshark` and `protonvpn`; adding one is a code change, though the `get_provider_env` abstraction means it's a small one. |
| F20 | P3 (bug, minor) | `--provider ""` | `cli.py` / `providers.py` | An empty `--provider ""` is silently ignored rather than rejected. |
| F21 | P3 (inconsistency, minor) | `Selection.country` | `instance.py` env vs `apply.py` doc | `_baked_selection` yields `country=""` for unset values, `Selection.from_doc` yields `None`; only masked by the fold in `.key` comparisons — a latent surprise for any future equality-dependent logic. |
| F22 | P3 (perf, worst-case) | IP probe timing | `ipinfo.py:169–240, 248–292` | Worst case a single `_probe` ≈ 20 s and `fetch_ip_info` ≈ 5.5 min (15 retries × (20 s + 2 s)); `CURRENT_EXIT_IP_RETRIES=1` helps `up` but `finish_connection` still polls the long way. |
| F23 | P3 (bug, minor) | bench candidates | `bench.py` | Hostname-less candidates carry `latency=None`; downstream ranking has to be None-tolerant in ways that are easy to get wrong later. |
| F24 | P3 (tests) | pyproject | `pyproject.toml` | Dependencies unpinned; `tomllib` tests are skipped on Python 3.10 via `importorskip`, silently reducing coverage there. |
| F25 | P3 (hygiene) | version drift risk | `README.md` vs `status` output | Minor: `README.md` exit-code table is prose; `status --json` and `ls --json` schemas are only documented in code docstrings, not in the README. |

---

## 5. Detailed findings

### F1 — `vpn logs` passes the container name where a service name is required (P0, likely bug)

`cli.py:515–522`:

```python
args: list[str] = ["logs"]
if follow:
    args.append("-f")
args.extend(["--tail", tail, current_instance().container])
compose(*args)
```

`docker compose logs` takes **service** names, not container names. The generated compose file (`instance.py:157–166` + `vpn.yml`) always names the single service `gluetun`; only `container_name` is swapped to the instance name. So:

- Instance named `gluetun` (the natural default): the container name *and* service name coincide → works.
- Any other instance (e.g. `--instance plan-a`): container is `plan-a`, service is `gluetun` (`container_name: plan-a`). Compose looks up `plan-a` among services via `project.GetServices(names)` (compose v5 source, `logs.go`) → **"no such service" error or empty output**.

This breaks `vpn logs` exactly for the flagship multi-instance case, and there is no test covering it (no compose interaction is unit-tested beyond mocked `run`).

**Fix options:** (a) pass the *service* name (`gluetun`) — compose will then print logs with the container name as prefix, matching what users expect; (b) better, when the instance container is not the default-named one, still pass the service name. Recommend (a) plus a test asserting the compose command uses the service name. *Needs a quick live check on a real Docker host to confirm the exact error text, but the semantics are unambiguous from compose's own source.*

### F2 — `ls --json` and `status --json` disagree on `control_server.enabled` (P1)

- `status --json` (`cli.py:224–232, 260`): `enabled = bool(sel and sel.provider)` — i.e., the control server actually responded with a valid selection.
- `ls --json` (`discovery.py:121–123`): `enabled = state == "running"` — i.e., the container merely exists in the `running` state.

Concretely: a running instance whose control server is unreachable (container up, gluetun wedged, port rebound) reports `enabled: true` in `ls --json` and `enabled: false` in `status --json`. Two consumers reading the same field from two commands get contradictory answers. The selection column has the same flavor: `ls` shows selection only when the control server answered, so a user staring at an `ls` row with `enabled: true` and `selection: null` has no explanation.

**Fix:** make `instance_records` probe the control server the same way `_status_doc` does (it already fetches `sel` — set `enabled` based on `sel is not None`, not state). One shared schema constant would prevent future drift.

### F3 — Registry-less instance falls back to probing `127.0.0.1:8000` (P1, misattribution risk)

`instance.py:202–203`:

```python
if control_port is None:
    control_port = BASE_CONTROL_PORT   # 8000
```

When an instance has no registry record and no published/known port, every command targets the control server at `127.0.0.1:8000`. If a *different* instance (or some unrelated service) happens to hold that port **and** speaks the gluetun control API, the CLI will happily read/write the wrong instance's selection. The comment in `_resolve_for_command` (`cli.py:147–149`) says the fallback exists so "imported/shared containers stay addressable" — but the fallback is unverifiable: nothing confirms the thing listening on 8000 is *this* instance. `_runtime_selection` then attributes a foreign selection to the requested name, and hot-swap commands can mutate the wrong container.

Note the recursion: `_resolve_for_command` additionally probes `container_control_port` when the registry is absent (`cli.py:161–164`), so the 8000 fallback only triggers when the container publishes no control port at all. That narrows the trigger, but a *stopped* foreign container or a missing published-port parse (e.g. IPv6-style port mappings the format string misses) still lands on port 8000.

**Mitigation:** when falling back to `BASE_CONTROL_PORT`, verify identity (e.g., match the container's `container_json` `Names`/`Id` against what the control server reports, or at minimum emit a loud warning). At minimum, keep the fallback but make it warning-gated.

### F4 — README/behavior mismatch on which commands mutate and which need the API key (P1)

`README.md:252` states:

> The commands that mutate or select the runtime config (`up`, `connect`, `bench`) refuse to run without it; read-only commands (`status`, `logs`, `ls`, `dns`, `update`, `down`) don't gate on it.

But three of those "read-only" commands **mutate** runtime state:

- `down` (`cli.py:500–508`) calls `control.set_vpn_status("stopped", ...)` and `compose down`.
- `dns on/off` (`cli.py:735–750`) calls `control.set_dns_status(target)`.
- `update` (`cli.py:753–762`) calls `control.trigger_updater()`.

None of them call `require_api_key` (only `up`, `connect`, `bench` do, at `cli.py:380, 469, 677`). Two ways to read this:

1. The README's factual claim ("`dns`/`update`/`down` are read-only") is wrong — the doc should be corrected; **or**
2. These mutating commands intentionally skip the gate (stopping the tunnel is arguably an operator action that shouldn't require the control API key to be present in the env). If so, the README rationale should be changed to "commands that *change the selection or start containers* require the key; lifecycle/DNS/update commands deliberately don't."

Either way, there is a real gap: an API key is required to start the VPN but not to stop it silently — and `dns off` (which disables DNS-over-TLS / modifies firewall state) needs no authentication at all. Worth a deliberate decision and a doc fix, not an accident.

### F5 — Ctrl-C handling only in `bench` (P1, polish/UX)

`bench` (`cli.py:703–704`) catches `KeyboardInterrupt` and exits cleanly with code 130 — and `bench.py:252–298` restores pre-bench settings on interrupt. `up` (`cli.py:344–440`), `connect` (`cli.py:451–497`), and `status` (`cli.py:551–604`) have no handler: Ctrl-C during a compose up, a probe loop, or a speed test prints a raw traceback (and in `up`'s case may leave the container mid-create). Users see `Traceback (most recent call last) ... KeyboardInterrupt`, which looks like a crash.

**Fix:** add a shared `except KeyboardInterrupt: raise SystemExit(130) from None` wrapper (click already converts `Abort` for prompts; a small decorator or a `main()`-level try/except would cover all commands uniformly — this also future-proofs new commands).

### F6 — API key can be sent over plaintext HTTP to an off-host address (P1, security)

`control.py:36–44`:

```python
def base_url() -> str:
    ...
    address = env_lookup("HTTP_CONTROL_SERVER_ADDRESS")
    return (address or current_instance().base_url).rstrip("/")
```

`HTTP_CONTROL_SERVER_ADDRESS` is documented (README) as user-overridable. If set to `http://some-host:8000`, the API key (`HTTP_CONTROL_SERVER_API_KEY`) is transmitted in cleartext over the network in the `X-API-Key` header (`control.py:58`), with no validation that the address is loopback and no warning that HTTP is being used. The default (`:8000` baked in `vpn.yml`) is loopback-bound, so the risk only appears when the user overrides — but the CLI composes the header itself, so it should at least warn or refuse non-loopback HTTP.

**Recommendation:** if the address is a URL that isn't `127.0.0.1`/`localhost`/`::1` and uses `http://`, print an explicit warning; optionally support `https://` properly. The key travels in the `X-API-Key` header (`control.py:58`), so the leak applies to that header.

### F7 — Port-adoption logic duplicated between `_resolve_for_command` and `up` (P2)

`_resolve_for_command` (`cli.py:161–164`) already does "registry-less + running → adopt published control port". `up` re-implements the same idea with slightly different conditions and ordering (`cli.py:367–377`, also handling the not-running → allocate case). The duplication is a drift hazard: F3's warning-gating fix would need to land in two places. Extract one helper, e.g. `_adopt_or_allocate_port(inst, ctl_port, env_port) -> Instance`.

### F8 — `cli.py` is drifting toward a monolith (P2)

762 lines split across command bodies, the status document builder, selection helpers, drift detection, and IP-status glue. Consequences visible in the coverage table: `cli.py` at 57 % is the lowest of the command layer and its tests are the most mock-heavy. Suggest extracting: `_status_doc` + schemas → `statusdoc.py` (shared with `ls --json` to fix F2), Ctrl-C handling → decorator, selection resolution/`_apply_request` helpers → `apply.py`/`servers.py`.

### F9 — `vpn ls` makes N+1 docker calls (P2, perf)

`instance_records` (`discovery.py:108–127`) calls per instance:
- `_state` → `container_status` → one `docker inspect`-ish call,
- `_control_port` → possibly `container_control_port` (another inspect),
- `_runtime_selection` → a control-server HTTP call,
- `consumers_of` → **another full `docker ps -a`** per instance.

Plus `_compose_projects()` runs `docker ps -a` once up front. Two full `docker ps -a` runs and one inspect per instance; with 5 instances that's ~12+ docker invocations for one `ls`. All the data needed is in a single `docker ps -a --format` (names, status, labels, network mode, port bindings). Worth one consolidated pass, especially since `consumers_of` is also used by `ls` in the *table* path on every row.

### F10 — Non-atomic `servers.json` cache write (P2)

`servers.py:175–177`:

```python
CACHE_FILE.write_text(json.dumps({...}))
```

A crash (or Ctrl-C, see F5) between `truncate` and full write leaves a corrupted cache. The reader (`_load_cache`) then fails to parse and — presumably — treats it as a miss and refetches, so the *impact* is a wasted refetch plus churn, not a hard failure; but with F5 fixed, a Ctrl-C mid-refetch could corrupt the cache and trigger a perpetual refetch loop until the file is cleaned. Standard fix: write to `CACHE_FILE.with_suffix(".tmp")` + `os.replace`.

### F11 — `logs -n/--tail` unvalidated string (P2)

`cli.py:514`: `@click.option("-n", "--tail", default="50", ...)` — the value is a `str` forwarded verbatim to `docker compose logs --tail`. `docker logs` accepts `all` or a number, but an arbitrary value like `abc` produces a confusing daemon error instead of a CLI error. Cheap fix: `type=click.IntRange(min=0)` (or a custom `all`-accepting type).

### F12 — `.env` parsing gaps (P2)

`config.py:87–99`: `read_env_file` splits lines on the first `=`, strips surrounding whitespace, and strips one pair of quotes — but does **not** strip inline `#` comments. `KEY=value # comment` keeps the comment verbatim in the value (potentially breaking gluetun env values), while `KEY=value` and quoted-value forms work. CRLF files are handled fine (both `.strip()` calls drop the trailing `\r`). The main real-world gap is inline comments; a `#` inside a quoted value should still be preserved. Worth matching the squall of dotenv implementations (or just documenting the supported subset).

### F13 — TOCTOU in `allocate_free_port` (P2)

`instance.py:251–256` + `_port_in_use` (a bind probe at `instance.py:245` area): the check-and-bind is not atomic — two `vpn up` runs for two instances racing can both observe port 8000 free and both pick it, then the second `compose up` fails on the published-port bind (or, worse, binds a *different* port than the registry claims). The failure mode is benign-ish (compose errors, user retries) but the error message will be confusing. Cheap mitigation: hold the bound socket until just before compose, or re-bind-check immediately before writing the registry.

### F14 — `CACHE_TTL` bound at import (P2)

`config.py:20`: `CACHE_TTL = int(os.getenv("GLUETUN_CACHE_TTL", "3600"))` is evaluated at import; changing it in `.env` (or mid-process) has no effect, unlike every other knobs respected per-instance. Minor but inconsistent with the per-instance `build_env` model.

### F15 — No way to forget an instance (P3, UX)

`vpn down` stops the container but leaves the registry record; `vpn ls` will then show the instance as `absent` forever, and there is no `vpn rm`/`forget` command. Multi-instance users will accumulate ghost rows. Suggest a `vpn rm --instance X` (or `down --rm`) that stops, removes, and deletes the registry entry + generated compose dir.

### F16 — Registry doesn't persist the provider (P3, UX)

The registry record (`instance.py`) stores name/control_port/env_file; `up` on a stopped instance requires `--provider` again (`cli.py:404–406`). Since the compose env bakes `VPN_SERVICE_PROVIDER`/`VPN_TYPE`, `up --recreate` could re-derive it from the container's env (like `_baked_selection`, `cli.py:205–216`). Either persist provider/protocol in the registry or reuse the baked env; today a stopped instance is a small memory tax on the operator.

### F17 — No `bench --json` (P3, feature)

`BenchReport` (`bench.py:103` area) carries structured data (winner, throughputs, action), but `bench` only renders a human table (`cli.py:708`). Given `status --json` and `ls --json` exist, `bench --json` is the obvious third wheel for scripting and comparison runs.

### F18 — No cache-refresh flag (P3, feature)

Server data is cached with `CACHE_TTL` and refetched only on expiry/miss. For bench runs against fresh server lists there's no `--refresh`; users must `rm` the cache or wait. Cheap: a flag on `up`/`connect`/`bench`.

### F19 — Only two hardcoded providers (P3, feature)

`providers.py:41–47` builds `PROVIDERS` for `("surfshark", "protonvpn")`. The abstraction is good (env maps per protocol), so adding providers is a code change of a few lines each, but there's no runtime extension point (config-driven provider definitions would be nice-to-have, not a blocker).

### F20 — `--provider ""` silently ignored (P3, minor bug)

An empty-string provider argument is falsy, so `up --provider ""` behaves like `up` with no provider, and `resolve_provider` will go down the "current protocol" path; the user's explicit empty value is silently discarded. Reject empty strings in the option validators (cheap: a custom `click` type that raises on `""`).

### F21 — `Selection.country` `""` vs `None` (P3, latent inconsistency)

- `_baked_selection` (`cli.py:211–215`): `env.get("SERVER_COUNTRIES") or env.get("VPN_COUNTRY")` → `""` when unset.
- `Selection.from_doc` (used by control-server reads): unset fields → `None`.

`Selection.key` folds values (verified: `Selection("surfshark","wireguard","","").key == Selection("surfshark","wireguard",None,None).key`), so current comparisons work — but any future code that checks `sel.country is None` (or serializes the dataclass directly) will diverge silently between the two sources. Normalize on one sentinel at construction.

### F22 — Worst-case probe latency (P3, perf)

`_probe` fans out 4 `docker exec` calls bounded by `PROBE_EXEC_TIMEOUT_S = 20` (`config.py:52`), i.e., worst case ~20 s per poll. `fetch_ip_info` defaults to `IP_FETCH_RETRIES = 15` × (`PROBE_TIMEOUT 8` + `IP_FETCH_DELAY 2`) → worst case ≈ 5.5 min per `finish_connection`. `current_exit_ip` uses retries=1 (`CURRENT_EXIT_IP_RETRIES`, `config.py:54`), so hot-swap verification is bounded, but `up`/`connect`/`status` cold-start verification can stall for minutes when providers are rate-limited or the network is slow. Consider: lower exec timeout, exponential backoff after the first failures, or early-exit when all four providers fail fast (they usually fail fast, not at timeout).

### F23 — Hostname-less bench candidates have `latency=None` (P3, minor)

In bench candidate building (hostname-less rows), latency stays `None`, and downstream ranking must tolerate None. It works today but is fragile; prefer a sentinel ranking key (`latency or inf`-style) at the point of use.

### F24 — Unpinned deps + `tomllib` skip on 3.10 (P3, tooling)

`pyproject.toml` has unpinned runtime deps (only `click` etc.), so CI can drift silently. The tests use `tomllib` via `importorskip` — on Python 3.10 they're silently skipped, shrinking effective coverage on that interpreter. Pin (or at least floor) the core deps and consider `tomli` for 3.10.

### F25 — JSON schemas documented only in code (P3, hygiene)

`status --json` and `ls --json` schemas live only in docstrings; README documents exit codes but not the JSON contract. F2 shows the danger of hand-maintained schemas. Consider: one shared schema doc (and the F2 fix) — consumers of `ls --json`/`status --json` deserve a stable contract note in README.

---

## 6. Security notes

1. **API key over plaintext HTTP (F6, P1).** Default config is loopback-only and safe; the override path is the risk. Warn/refuse on non-loopback `http://`.
2. **Unauthenticated state mutation (F4, P1-related).** `down`, `dns off`, and `update` mutate runtime state without the API key. Evaluate whether that's acceptable; it's at least a documented decision. `dns off` reconfigures the DNS resolver/firewall of the container — arguably higher impact than `status`.
3. **Control server binds loopback, compose file pins `127.0.0.1` (**`vpn.yml:10`**).** Good by default.
4. **Sensitive env masking in `--debug` (`cli.py:95–103`).** `_log_env` masks keys containing SENSITIVE_KEY_PARTS; verified the mask list includes the obvious ones (key, password, token). Watch out for future keys like `OPENVPN_PRIVATE_KEY` variants — the mask list must be kept in sync (a test asserting masking for a curated list of key names would help).
5. **`docker exec` with full container identity.** All exec paths use exact instance names resolved from registry/env, not user strings — no injection surface seen.
6. **No secrets written to disk.** Env is passed via compose `environment:`/`.env` — consistent with the gluetun model; registry stores no secrets (verified record schema).

---

## 7. Testing gaps

| Gap | Related finding | Note |
|---|---|---|
| No test that compose calls use the right identifiers (service vs container) | F1 | A mocked `compose()` assertion for `logs` (and `up`/`down`) would have caught F1; add one parametrized over non-default instance names. |
| No test for `ls --json` vs `status --json` schema agreement | F2 | Property-style test: same instance state → same `enabled` semantics. |
| No test for port fallback to `BASE_CONTROL_PORT` warning/identity check | F3 | Live docker needed for the full path; unit-test the decision function once extracted. |
| Ctrl-C behavior untested (except bench) | F5 | Test the shared wrapper with a raising command; simulate `KeyboardInterrupt` through the click entrypoint. |
| `read_env_file` edge cases untested | F12 | Add cases: inline comments, quoted `#`, `KEY=` empty, quotes, unicode. |
| Cache corruption path untested | F10 | Feed an intentionally truncated `servers.json`; assert refetch + rewrite. |
| `logs --tail` invalid input untested | F11 | Assert a friendly error for `-n abc`. |
| Race in `allocate_free_port` untested | F13 | At least a test that the port-bound check happens right before registry write. |
| Coverage floors | — | `docker.py` 51 % and `cli.py` 57 % are the low spots; both are the glue where regressions like F1 live. |

---

## 8. Prioritized workplan

Rough effort in a half-day unit for an engineer who knows the codebase.

### P0 — fix the broken command first

| Task | Effort |
|---|---|
| **F1** Fix `vpn logs` to pass the compose *service* name (`gluetun`); add a mocked-compose regression test parametrized over instance names; live-verify on a Docker host. | ~0.5 d |

### P1 — correctness and trust

| Task | Effort |
|---|---|
| **F2** Unify `control_server.enabled` semantics: base it on the control probe in both `ls --json` and `status --json`; extract a shared schema builder. | ~0.5 d |
| **F3** Warn (or identity-check) when falling back to `BASE_CONTROL_PORT` for registry-less instances; thread through `_resolve_for_command` (which already partially handles it). | ~0.5 d |
| **F4** Decide the API-key policy for `down`/`dns`/`update`; fix README wording to match reality either way. | ~0.25 d |
| **F5** Add a shared `KeyboardInterrupt → SystemExit(130)` wrapper at the entrypoint (covers `up`, `connect`, `status`, and future commands). | ~0.25 d |
| **F6** Warn/refuse non-loopback plaintext-HTTP control addresses; keep the loopback default silent. Add a masking-keys regression test while here. | ~0.5 d |

### P2 — robustness and design debt

| Task | Effort |
|---|---|
| **F7 + F8** Extract shared `_adopt_or_allocate_port` and a `statusdoc.py` schema module (drains F2 too); shrink `cli.py`. | ~1 d |
| **F10** Atomic cache write (tmp + `os.replace`). | 0.1 d |
| **F11** Validate `--tail` (int or `all`). | 0.1 d |
| **F12** Strip inline comments (respecting quoted `#`) in `read_env_file` + tests. | ~0.25 d |
| **F13** Hold-while-check in `allocate_free_port` (re-bind check right before registry write). | ~0.25 d |
| **F14** Read `CACHE_TTL` per call, not at import. | 0.1 d |
| **F9** Consolidate `vpn ls` docker calls into one `docker ps -a` pass. | ~0.5 d |

### P3 — UX, features, hygiene (pick what matters)

| Task | Effort |
|---|---|
| **F15** `vpn rm` / `down --rm` to forget instances; **F16** persist provider in registry (or reuse baked env). | ~0.5 d |
| **F17** `bench --json`; **F18** `--refresh` cache flag. | ~0.5 d each |
| **F20** Reject empty `--provider ""`; **F21** normalize `Selection` empty-country sentinel. | ~0.25 d |
| **F22** Smarter probe backoff / shorter exec timeout. | ~0.5 d |
| **F23** Sentinel ranking for None latency in bench. | 0.1 d |
| **F24** Pin deps, add `tomli` for 3.10, or bump min to 3.11. | 0.25 d |
| **F19** Provider config-extension (nice-to-have). | ~1 d |
| **F25** Document the JSON schemas and exit-code table in README. | 0.25 d |

**Suggested order:** P0 → P1 (a "consistency and trust" pass) → P2 (robustness + extraction of the schema/port-adoption helpers) → P3 as a backlog. The P2 extraction work is best done *before* the P3 features so they build on the shared helpers rather than the monolith.

---

## 9. Verification appendix

**Environment:** no Docker daemon available; all Docker-touching behavior verified by code reading and by reading the compose v5 (Go) source rather than by execution. Everything below *was* executed.

| Claim | How verified | Result |
|---|---|---|
| `Selection.key` fold masks `""` vs `None` country | Ran: `Selection("surfshark","wireguard","","")` vs `Selection("surfshark","wireguard",None,None)` `.key` comparison | Equal |
| Registry-less instance resolves to `BASE_CONTROL_PORT` 8000 | Ran `resolve_instance("ghost-imported", control_port=None)` with empty registry | `control_port == 8000` |
| `parse_server_selection("[surfshark/wireguard] Japan - ")` city | Ran the parser | city → `""` (not `None`) |
| `docker ps -a` format/labels parse | Code-read `discovery.py` | Confirmed |
| Compose template service/container naming | Read `vpn.yml` + `render_compose` | Service always `gluetun`; only `container_name`/port swapped |
| compose `logs` semantic (service names only) | compose v5 `logs.go`: `Use: "logs [OPTIONS] [SERVICE...]"`; names passed to `project.GetServices` which matches service names | Confirmed — F1 is high-confidence, still flagged for live re-check |
| API-key gate coverage | `grep require_api_key` → only `up`/`connect`/`bench` call it (`cli.py:380, 469, 677`) | Confirmed F4 |
| `enabled` semantics divergence | Read `discovery.py:121–123` vs `cli.py:224–232, 260` | Confirmed F2 |
| Worst-case probe timing | `config.py:49–54`: retries 15 × (exec 20 s + delay 2 s) | ≈ 5.5 min worst case |
| Version sync | Read `src/vpn/version.py` vs `pyproject.toml` | Both 0.2.6, `vpn --version` matches |
| Test suite / lint / types | Ran `pytest` (347 passed), `ruff check`, `mypy` | Clean |