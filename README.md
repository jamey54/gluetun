# epoxy

Small Python CLI for managing VPN containers, backed by [Gluetun](https://github.com/qdm12/gluetun) images.
Container lifecycle goes through docker compose; every selection change (provider, protocol,
country, city) hot-swaps at runtime through the container's control server
(`GET/PUT /v1/vpn/settings`) — single-digit-second switches, no container restarts.

## Supported providers

| Provider  | WireGuard | OpenVPN |
|-----------|-----------|---------|
| Surfshark | ✓         | ✓       |
| ProtonVPN | ✓         | ✓       |

All providers can be active simultaneously — their servers appear side by side in `epoxy connect --list` and the picker, tagged with their protocol. A provider's protocol is listed only when its credentials are present in `.env`.

## Requirements

- Python 3.10+
- A Gluetun image (pinned to `qmcgaw/gluetun:v3.41.3` by default; the settings route is mandatory — run `epoxy up --pull` to (re)pull it, or override with `EPOXY_IMAGE`)
- [click](https://click.palletsprojects.com/), [prompt_toolkit](https://python-prompt-toolkit.readthedocs.io/) and [rich](https://rich.readthedocs.io/) — installed automatically via `pip install .`

## Install

```bash
pip install .
```

This installs the `epoxy` command (and its dependencies) into your environment.

## Shell completion

```bash
epoxy install          # append completion to ~/.bashrc / ~/.zshrc / fish config
epoxy install --print  # preview the snippet without writing
```

`--shell bash|zsh|fish` overrides the `$SHELL` auto-detection and `--rc-file`
picks the file (defaults per shell). Re-running is a no-op when the block is
already there; replacing a diverged block needs `--force`. Or wire it manually:

```bash
eval "$(_EPOXY_COMPLETE=bash_source epoxy)"
```

Reload your shell and tab completion works for commands and options.

## Setup

```bash
cp .env.sample .env
# edit .env with your WireGuard credentials
```

Each provider has its own prefixed credentials in `.env` (the file holds secrets only):

```
SURFSHARK_WIREGUARD_PRIVATE_KEY=...
SURFSHARK_WIREGUARD_ADDRESSES=...
PROTONVPN_WIREGUARD_PRIVATE_KEY=...
PROTONVPN_WIREGUARD_ADDRESSES=...
HTTP_CONTROL_SERVER_API_KEY=...
```

You only need to set credentials for providers you actually use.

## Commands

| Command | Description |
|---------|-------------|
| `epoxy --version` | Print the exact version (e.g. `epoxy 0.5.0`) and exit `0` — derived from `src/epoxy/version.py`, kept in sync with `pyproject.toml` |
| `epoxy up [--instance NAME] [--ctl-port P] [--env-file F] [--provider --protocol --country --city] [--pull] [--recreate] [--no-speedtest]` | Start (or verify) the VPN; apply any requested location via hot-swap |
| `epoxy connect [--instance NAME] [--provider --protocol --country --city] [--list] [--no-speedtest]` | Hot-swap to another server; no arguments opens the picker |
| `epoxy status [--instance NAME] [--all] [-s SIZE] [--no-speedtest] [--json]` | Container state, effective selection, public IP, speed test |
| `epoxy ls [--instance NAME] [--json]` | List instances (registry + `epoxy-*` compose containers): state, selection, control port, consumers, start time |
| `epoxy down [--instance NAME] [--all]` | Stop the VPN container (registry record kept; shows as `absent` in `epoxy ls`) |
| `epoxy rm [--instance NAME] [--all] [-f/--force]` | Remove the container/network and delete the registry record, compose file, and lockfile; refuses when consumers share the instance's network unless `--force` |
| `epoxy logs [--instance NAME] [--all] [-f] [-n N]` | Show container logs |
| `epoxy bench [--instance NAME] [--connect]` | Benchmark locations and report the fastest (keeps current unless `--connect`) |
| `epoxy dns [--instance NAME] [--all] [on\|off]` | Show or toggle the DNS-over-TLS resolver |
| `epoxy update [--instance NAME] [--all]` | Trigger a server list update |
| `epoxy install [--shell SHELL] [--rc-file FILE] [--print] [--force]` | Wire shell completion into your rc file (bash/zsh/fish) |

`--instance` is the first option of every command. Set `EPOXY_INSTANCE` to avoid repeating it. See [Instances](#instances).

### up vs connect

- **`up`** ensures the container exists and runs. On a stopped container it creates it via compose (requires `--provider`; credentials come from `.env`). Once running, explicit flags are applied as a runtime hot-swap — including cross-provider/protocol switches. With no flags on an already-running container it only verifies the tunnel. A running container whose control server never answers is an error (exit 1), not a silent success.
- **`connect`** requires a running container and *only* hot-swaps. With no arguments it opens the interactive picker; `--list` prints the servers table instead.
- **`up --pull`** pulls the pinned image and recreates the container. **`up --recreate`** recreates from compose/`.env` config without pulling — the escape hatch if a swap ever leaves the tunnel stuck.

Both commands share the same target resolution:

- `--country` replaces the location outright (`--city` may accompany it).
- A lone `--city` keeps the current country.
- Switching `--provider` without a location drops the old country/city.
- When the requested target already matches the running state, nothing is swapped ("Already on").

## Provider details

### Surfshark

WireGuard credentials: Surfshark admin panel → Manual setup → WireGuard.

```bash
epoxy up --provider surfshark
```

OpenVPN credentials: admin panel → Manual setup → OpenVPN config (service username/password).

```bash
epoxy connect --protocol openvpn   # switch the running tunnel's protocol
```

### ProtonVPN

WireGuard credentials: generate at [account.proton.me/vpn/WireGuard](https://account.proton.me/vpn/WireGuard).

```bash
epoxy up --provider protonvpn
```

OpenVPN credentials: [account.proton.me/vpn/OpenVPN](https://account.proton.me/vpn/OpenVPN) → OpenVPN username / password.

```bash
epoxy up --provider protonvpn --protocol openvpn
```

`--protocol` defaults to the running one, else wireguard. Credentials for whichever pair you pick are injected from `.env` into the settings document, so provider and protocol switches never require a restart.

## Server selection

`epoxy connect --list` lists all servers in an aligned table (provider, protocol, country, city, server) for every provider/protocol pair with valid credentials in `.env`.

`epoxy connect` with no arguments opens an interactive picker showing the same columns, with live filtering and keyboard navigation:

- **Filtering** — type to filter, matching any column (accent-insensitive). `Tab`/`Shift-Tab` cycle an active filter *column* (Provider, Protocol, Country, City); while one is active, typing matches only within it and `←`/`→` cycle through that column's distinct values (e.g. `Tab`, `→` — one `Tab` activates the Provider column, then `→` cycles ProtonVPN → Surfshark; no typing needed). `Esc` exits column mode (or clears the query).
- **Navigation** — `↑`/`↓` or `Ctrl-N`/`Ctrl-P` to move, `PgUp`/`PgDn` for pages, `Home`/`End` for first/last, `Enter` to select, `Ctrl-C`/`Ctrl-Q` to cancel.

The selected row's provider *and* protocol are hot-swapped immediately.

## Runtime selections and drift

Selections made through the control server live at **runtime only** — `.env` stays secrets-only. Consequences:

- A hot-swapped location survives container restarts (`restart: always`) but is lost when the container is recreated (`epoxy up --pull`, `epoxy up --recreate`) or removed (`epoxy down`); recreation reverts to whatever the compose file interpolates from `.env`.
- `epoxy status` shows the effective (runtime) selection; when it differs from the selection baked into the container's env at create time, `epoxy status --json` reports `"drift": true`.
- A fresh `epoxy up --country/--city` starts the container first and applies the location via hot-swap, waiting up to ~15s for the just-started control server; a server that never comes up is a friendly `Could not switch to ...` error (exit 1), never a traceback.

## Connection verification

Verification is **leak-first**: a connection counts as up only when the exit IP observed from inside the container differs from the host's bare public IP. The bare IP is fetched host-side once per run (overridable with `EPOXY_REAL_IP` for testing; when unavailable, leak detection degrades to country heuristics only).

After connecting (and after every swap), the CLI probes the public IP from inside the container and reports one of three verdicts. The probe mirrors the upstream resilient fetch: four echo services (ipinfo.io, Cloudflare `one.one.one.one/cdn-cgi/trace`, ifconfig.co, ip2location) are queried in parallel from inside the container and the most-agreed result wins, so a rate-limited provider (e.g. ipinfo returning HTTP 429) is absorbed by the rest instead of stalling the retry loop. The verdict notes via yellow when the IP was confirmed by a service other than ipinfo:

- **Green** — real VPN exit in the requested country.
- **Yellow warning** — real VPN exit, but it geolocates elsewhere than requested (common with provider "virtual locations"). You stay connected and the speed test still runs.
- **Red / leak** — the exit IP equals your bare connection's IP; the speed test is skipped. When no exit IP can be read at all the connection is treated as unverified (no leak marker) and the speed test is skipped too.

Swaps additionally exclude the previous exit IP from acceptance, so a failed swap that silently keeps routing through the old server is reported as `no reconnect` rather than mistaken for success. IP echo services report ISO 3166-1 alpha-2 codes (`AU`), normalized to full names before comparing. Country is advisory only: it never gates success — the IP change does. `epoxy status` and a flag-free `epoxy up` on an already-running instance compare against the instance's *running* country, so a drifting exit (e.g. after a virtual-location server was removed) shows as a yellow geo warning instead of a blind green.

## Speed test

`up`, `connect` and `status` run a download speed test after a verified connection (green or yellow verdict). It downloads 25 MB from Cloudflare inside the container — all traffic goes through the VPN tunnel. Skip it per invocation with `--no-speedtest`, or change the size with `epoxy status -s 100`. On a leak or unreadable IP, the speed test is skipped with a message.

## Benchmark

`epoxy bench` benchmarks locations across **all credentialed providers/protocols** and reports the fastest. It does **not** connect by default — your current location is kept unless you pass `--connect`:

```bash
epoxy bench                        # every credentialed provider/protocol; keep current
epoxy bench --country Japan        # one country, every provider
epoxy bench --provider surfshark   # one provider's countries
epoxy bench --protocol openvpn     # one protocol, every provider
epoxy bench --connect              # benchmark, then connect to the fastest
epoxy bench -c 4                   # bench 4 candidates at once on temp containers
```

How it runs:

1. **Latency prescreen** — parallel TCP-connect probes (port 443, host-side) rank every candidate location; unreachable ones sort last.
2. **Screening** — the top `--top` (default 12) locations are tested with a `--scan-size` MB (default 10) download. Each must prove an IP change (exit IP must differ from your bare IP *and* the previous exit — leaks and failed swaps are marked `leak` / `no reconnect`). Exits that geolocate outside the requested country are flagged `geo: <country>` but still tested.
3. **Finals** — the best 3 are re-tested with the full `-s/--size` MB (default 25) download.
4. **Result** — without `--connect` the pre-bench settings are restored and the table reports the winner. With `--connect` the winner is adopted after a final re-check of the exit IP — unless it is already the active location, in which case nothing is re-swapped (it is kept and re-checked in place). In either case Ctrl-C at any point restores the pre-bench settings.

The default `-c/--concurrency 1` hot-swaps the running container for every test. Raise it (e.g. `-c 4`) to run the screening and final stages as batches of concurrent tests, each candidate on its own **temporary one-off container**; your live connection is then left untouched until the winner is connected. All temporary containers are removed after each batch, and aborted runs tear them down too.

All swaps go through the same locked runtime engine as `connect`: concurrent CLI invocations (e.g. a bench while someone picks a server) serialize on a lockfile instead of clobbering each other's settings.

Notes:

- Candidates default to every credentialed provider/protocol; use `--provider`, `--protocol`, or `--country` to narrow, or `-n/--max-candidates` to cap the ones entering the latency stage.
- Parallel mode (`-c > 1`) is limited by the credentials your provider permits: if a provider caps simultaneous sessions, some candidates will simply be reported as failures (`no public IP`) while the rest keep benching — it won't abort the run. Temporary containers inherit the resolved instance env (including `--env-file`), snapshotted before the run, so no exported variables are needed.
- Bench state is applied at runtime only — recreating the container (see [Runtime selections and drift](#runtime-selections-and-drift)) reverts to the env-file selection.

## Instances

epoxy 0.3 runs several independent VPN containers side by side, each its own *instance*. An instance is identified by a name — its docker container name — and owns:

- a container named exactly `<instance>` (compose project `epoxy-<instance>`);
- a control server published on `127.0.0.1:<port>`;
- a lockfile `~/.cache/epoxy/locks/<instance>.lock` (swaps/benches serialize per instance only; different instances run concurrently);
- an optional `--env-file` replacing `.env` for that instance;
- a registry record `~/.cache/epoxy/instances/<instance>.json` (control port + env file).

Every command requires an instance. Resolution order: `--instance NAME` → `EPOXY_INSTANCE` env var → interactive choice → error. On an interactive terminal with no `--instance` and no env var, commands that target an instance (`status`, `down`, `connect`, `logs`, `bench`, `dns`, `update`, `up`) ask you to pick one: the sole known instance is used automatically, otherwise a picker lists them by name and state — filter it by typing, and the column-filter keys (`Tab`, `←`/`→`) are inert there, since only the name is a real column. Non-interactive runs (pipes, scripts) keep the usage error — automation must always name its instance explicitly. `epoxy ls` always lists everything and never prompts.

Container names are **exact matches only**: epoxy never touches a container other than the one named after the instance, so a shared container owned by another tool is never matched.

### Control port

Each instance publishes the control server on `127.0.0.1:<port>`. **Every** command resolves the port in this order:

1. `--ctl-port HOST_PORT`
2. `EPOXY_CTL_PORT` (from the instance's env)
3. the registry record (`~/.cache/epoxy/instances/<instance>.json`)
4. a registry-less instance's *actually published* host port, read live from Docker — i.e. an imported or shared container created outside epoxy
5. `8000` — only when the container has no published port

`epoxy up` on a brand-new instance auto-allocates a free port in `[8000, 9000]`, persists it to the registry, and writes it into the compose file. An explicit `--ctl-port`/`EPOXY_CTL_PORT` on an *existing* instance is persisted too, so the override is remembered by the next command instead of reverting to the stale record. A registry-less container keeps its own port: `up` adopts it and every other command targets it, so a shared container created outside epoxy stays addressable — `epoxy status --json` on such an instance reports its real `control_server.port` instead of a blind `8000`. `up` never *creates* a registry record for a container it did not create, so an imported container stays registry-less. The control server always listens on port `8000` *inside* the container; only the published host port changes.

`bench -c N` temporary one-off containers never publish host ports.

Each instance's generated compose file declares exactly one service, always named `epoxy`, while `container_name` is set to the instance name. The two are different things: `docker compose` verbs take the *service* name, while `docker` container operations take the container name. So a container named `plan-a` is reached as service `epoxy` under project `epoxy-plan-a`. epoxy passes the service name to compose and the container name to docker; the instance name is only ever used for the latter.

All control-server traffic (hot-swap `GET/PUT /v1/vpn/settings`, DNS, updater, status) targets the instance's own published port — never a hardcoded `8000`.

### Per-instance env files

Every instance reads its env from `./.env` by default. For a dedicated instance, pass `--env-file <path>`: that file **replaces** `.env` for the instance (compose `--env-file` semantics), while the process environment still fills anything it doesn't set. This lets different instances use different providers/credentials.

### Listing instances

`epoxy ls [--json]` enumerates instances from the registry and from containers whose compose project starts with `epoxy-`, reporting per-instance state, selection, control-server port, *consumers* — containers sharing the instance's network namespace (`NetworkMode == container:<instance>`; Docker records the reference as the container's name or its ID, both are matched) — and when each instance was started. Rows are ordered by start time, oldest instance first; instances without a start time (absent, or never started) sort last. `STARTED` is rendered in local time; under `--json` it is the raw RFC3339 timestamp (`"started_at"`), or `null` when unknown.

```text
$ epoxy ls
INSTANCE   STATE    CONTROL  SELECTION                             CONSUMERS        STARTED
epoxy    running  8000     surfshark/wireguard → Germany         firefox-app      2026-09-19 09:00:00
```

### Acting on all instances

`status`, `down`, `rm`, `logs`, `dns`, and `update` accept `--all` to act on every known instance (alphabetical) instead of one:

```text
$ epoxy status --all --no-speedtest
== epoxy ==
  Container   epoxy (running)
  ...
== plan-a-epoxy ==
  Container   plan-a-epoxy (stopped)
```

Rules:

- `--instance` and `--all` together are a usage error (exit `2`). `--all` ignores `EPOXY_INSTANCE` and never prompts.
- `up`, `connect`, and `bench` take no `--all`: applying one selection to every instance is never what you want — target them explicitly. `ls` already lists everything.
- Failures are per instance: the run continues past a failing instance, reports it as `<name>: <error>`, and exits `1` when any instance failed. With no known instances, commands print `(no instances)` and exit `0`.
- `status --all --json` emits an `{"instances": [...]}` envelope (one status document per instance, same schema as `status --json`); it exits `1` when any instance trips the single-instance exit-1 rules.
- `logs --all` prints a `== <name> ==` header per instance; `--follow` cannot be combined with `--all` (exit `2`).
- `rm --all` applies the consumer guard per instance: shared instances are skipped (reported, exit `1`) unless `--force` is passed.

## Configuration

Secrets live in `.env` in the working directory (copy `.env.sample` to get started). Other settings are overridable via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `EPOXY_INSTANCE` | *(required for scripts)* | Instance name; pass `--instance` or set this. Omitting both asks interactively on a terminal (pick from the known instances); non-interactive runs fail with a usage error. |
| `EPOXY_CTL_PORT` | unset | Control-server host port for the resolved instance (equivalent to `--ctl-port`; must be `1`–`65535`, and an empty value counts as unset) |
| `EPOXY_CACHE_TTL` | `3600` | Server cache TTL (seconds); missing or non-numeric values fall back to the default |
| `EPOXY_IMAGE` | `qmcgaw/gluetun:v3.41.3` | Container image ref for compose/pull/server-fetch (bump deliberately after checking release notes) |
| `EPOXY_DEBUG` | unset | Set to enable debug output (same as `--debug`) |
| `EPOXY_REAL_IP` | unset | Override the host's bare public IP used for leak detection (for testing); without it the IP is fetched once per run, and leak detection degrades to country heuristics when unavailable |

### Credentials

A provider/protocol pair only appears in listings and can only be started when all of its *required* variables are set.

| Provider | Protocol | Variables | Required |
|----------|----------|-----------|----------|
| Surfshark | WireGuard | `SURFSHARK_WIREGUARD_PRIVATE_KEY`, `SURFSHARK_WIREGUARD_ADDRESSES` | private key |
| Surfshark | OpenVPN | `SURFSHARK_OPENVPN_USER`, `SURFSHARK_OPENVPN_PASSWORD` | both |
| ProtonVPN | WireGuard | `PROTONVPN_WIREGUARD_PRIVATE_KEY`, `PROTONVPN_WIREGUARD_ADDRESSES` (always `10.2.0.2/32`) | both |
| ProtonVPN | OpenVPN | `PROTONVPN_OPENVPN_USER`, `PROTONVPN_OPENVPN_PASSWORD` | both |

`HTTP_CONTROL_SERVER_API_KEY` (any random string) is **required** — it authenticates the container's HTTP control server, which the CLI exposes on `127.0.0.1:<port>` only (per instance, see [Instances](#instances)). The commands that mutate or select the runtime config (`up`, `connect`, `bench`) refuse to run without it; read-only commands (`status`, `logs`, `ls`, `dns`, `update`, `down`) don't gate on it.

### Set automatically

These are managed by the CLI at container creation time — never define them yourself: `VPN_SERVICE_PROVIDER`, `VPN_TYPE`, `WIREGUARD_PRIVATE_KEY`, `WIREGUARD_ADDRESSES`, `OPENVPN_USER`, `OPENVPN_PASSWORD` (mapped from your provider credentials). A location may be baked via `.env` `SERVER_COUNTRIES`/`SERVER_CITIES` (the compose template interpolates them); after creation, all *runtime* selection changes happen through the control server, and `epoxy up` verifies a fresh start — or a `--recreate` — against the baked location when present.

## Consumer API (dockerstrator)

This is the contract `dockerstrator` consumes from `epoxy`. Stable schemas — additions only, never removals or renames.

### Instance identity and naming

Instance names follow docker-safe rules (`^[a-zA-Z0-9][a-zA-Z0-9_.-]*$`); anything else is a usage error (exit `2`). The container name is **always** the instance name and is never derived from the compose project. The compose project is pinned to `epoxy-<instance>` via `docker compose -p`, independent of the file location.

Resolution order for every command: `--instance NAME` → `EPOXY_INSTANCE` → interactive choice (TTY only) → error. There is no default instance; a non-interactive run with neither flag is a usage error (exit `2`).

### Exit codes

| Code | Meaning |
|------|---------|
| `0`  | success |
| `1`  | scripted error / VPN failed / leak (verdict in JSON under `--json`) |
| `2`  | usage error |

Other non-zero codes are unspecified. `epoxy up` and `epoxy connect` return `1` when the connection cannot be verified; `epoxy status --json` returns `1` when `leak` is `true` or the control server is unreachable while the container is running/restarting; it returns `0` for any other emitted JSON (probe health failures are reported in `last_error`, never as a leak). `epoxy up` applies the same rule as `status`: a container that is *running* but whose control server cannot be reached returns `1`, whether or not a location was requested — it waits up to ~15s first, so a container that is merely mid-restart is not failed, but one that never answers is a friendly `Cannot read runtime settings` error rather than a success epoxy could not act on. `epoxy bench` returns `1` (friendly message, no traceback) when the control server becomes unreachable mid-run.

### The calls dockerstrator makes

| Purpose | Command |
|---------|---------|
| Ensure shared container is running (idempotent; verify-only when already up) | `epoxy up --instance epoxy` |
| Shared container health probe | `epoxy status --json` |
| Capability probe (is epoxy 0.3+ implemented?) | `epoxy ls --json` (or `epoxy --version` for the exact version) |
| Create a dedicated instance (creds from `.env`) | `epoxy up --instance <plan>-epoxy --provider P [--protocol T] [--country C] [--city Ci]` |
| Verify a dedicated instance after create | `epoxy status --instance <plan>-epoxy --json` |
| Tear down when the plan container is removed | `epoxy down --instance <plan>-epoxy` |

dockerstrator rule: if `epoxy ls --json` exits non-zero or reports an unknown flag, treat epoxy as pre-0.3 and hide the *dedicated* container option (shared-only falls back to plain `epoxy up`). Since 0.3.0 every call names its instance explicitly (`--instance` or `EPOXY_INSTANCE`) — there is no default instance anymore. dockerstrator keeps its own container inventory from `docker ps`; `epoxy ls` is used only for instance/control-port/selection state.

`--json` output is deterministic single-line JSON on stdout (no colors, no progress). `--instance` filters `epoxy ls` output to one instance.

### `epoxy status --json`

```json
{
  "instance": "epoxy",
  "container_name": "epoxy",
  "image": "qmcgaw/gluetun:v3.41.3",
  "state": "running",
  "selection": { "provider": "surfshark", "protocol": "wireguard", "country": "Japan", "city": "Tokyo" },
  "drift": false,
  "control_server": { "port": 8000, "enabled": true },
  "exit_ip": { "ip": "1.2.3.4", "country": "Japan" },
  "leak": false,
  "verified": true,
  "last_error": null
}
```

- `state` — `running` | `starting` | `stopped` | `absent`.
- `selection` — the runtime selection (control server) or `null` when the server is unreachable or the instance isn't up.
- `drift` — `true` when the runtime selection differs from the selection baked into the container's env at create time (i.e. it was hot-swapped).
- `control_server` — the instance's *resolved* host port (registry record, else the container's published port when the instance has no registry record) and whether the control server responded.
- `exit_ip` — `{ip, country}` observed from inside the container, or `null` (probed only while `running`).
- `leak` — `true` when the instance is `running` and the observed exit IP equals the host's bare public IP; `false` otherwise. A probe that reads no exit IP at all is probe health, not a leak: it is reported in `last_error` with `leak: false`.
- `verified` — `true` when the exit verifiably differs from the host's bare IP (or matches the requested country while the bare IP is unknown); `false` otherwise, including when the tunnel is merely stopped or the probe failed.
- `last_error` — human-readable failure detail (e.g. control server unreachable), else `null`.

### `epoxy ls --json`

```json
{
  "instances": [
    {
      "instance": "epoxy",
      "container_name": "epoxy",
      "state": "running",
      "selection": { "provider": "surfshark", "protocol": "wireguard", "country": "Japan", "city": "Tokyo" },
      "control_server": { "port": 8000, "enabled": true },
      "consumers": ["firefox-app", "squiz-dev"],
      "started_at": "2026-09-19T09:00:00.123456789Z"
    }
  ]
}
```

- `instances` — one entry per known instance; `state` uses the same values as `status --json`. `selection` and `control_server` are `null` when unknown. `consumers` lists containers sharing the instance's network (`NetworkMode == container:<container_name>`; containers attached by name or by the instance's container ID are matched). `started_at` is the instance's last start time (Docker `State.StartedAt`, RFC3339), or `null` when the instance is absent or has never started; records are ordered by `started_at`, oldest first, unknown last.
