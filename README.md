# vpn

Small Python CLI for managing a [Gluetun](https://github.com/qdm12/gluetun) VPN container.
Container lifecycle goes through docker compose; every selection change (provider, protocol,
country, city) hot-swaps at runtime through gluetun's control server
(`GET/PUT /v1/vpn/settings`) — single-digit-second switches, no container restarts.

## Supported providers

| Provider  | WireGuard | OpenVPN |
|-----------|-----------|---------|
| Surfshark | ✓         | ✓       |
| ProtonVPN | ✓         | ✓       |

All providers can be active simultaneously — their servers appear side by side in `vpn connect --list` and the picker, tagged with their protocol. A provider's protocol is listed only when its credentials are present in `.env`.

## Requirements

- Python 3.10+
- A current gluetun image (the settings route is mandatory; old images are not supported — run `vpn up --pull` to update)
- [click](https://click.palletsprojects.com/), [prompt_toolkit](https://python-prompt-toolkit.readthedocs.io/) and [rich](https://rich.readthedocs.io/) — installed automatically via `pip install .`

## Install

```bash
pip install .
```

This installs the `vpn` command (and its dependencies) into your environment.

## Shell completion

Add to your `.bashrc`:

```bash
eval "$(_VPN_COMPLETE=bash_source vpn)"
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
| `vpn --version` | Print the exact version (e.g. `vpn 0.2.6`) and exit `0` — derived from `src/vpn/version.py`, kept in sync with `pyproject.toml` |
| `vpn up [--instance NAME] [--ctl-port P] [--env-file F] [--provider --protocol --country --city] [--pull] [--recreate]` | Start (or verify) the VPN; apply any requested location via hot-swap |
| `vpn connect [--instance NAME] [--provider --protocol --country --city] [--list]` | Hot-swap to another server; no arguments opens the picker |
| `vpn status [--instance NAME] [-s SIZE] [--no-speedtest] [--json]` | Container state, effective selection, public IP, speed test |
| `vpn ls [--instance NAME] [--json]` | List instances (registry + `vpn-*` compose containers) and their consumers |
| `vpn down [--instance NAME]` | Stop the VPN container |
| `vpn logs [--instance NAME] [-f] [-n N]` | Show container logs |
| `vpn bench [--instance NAME] [--connect]` | Benchmark locations and report the fastest (keeps current unless `--connect`) |
| `vpn dns [--instance NAME] [on\|off]` | Show or toggle the DNS-over-TLS resolver |
| `vpn update [--instance NAME]` | Trigger a server list update |

`--instance` is the first option of every command. Set `GLUETUN_INSTANCE` to avoid repeating it. See [Instances](#instances).

### up vs connect

- **`up`** ensures the container exists and runs. On a stopped container it creates it via compose (requires `--provider`; credentials come from `.env`). Once running, explicit flags are applied as a runtime hot-swap — including cross-provider/protocol switches. With no flags on an already-running container it only verifies the tunnel.
- **`connect`** requires a running container and *only* hot-swaps. With no arguments it opens the interactive picker; `--list` prints the servers table instead.
- **`up --pull`** pulls the latest image and recreates the container. **`up --recreate`** recreates from compose/`.env` config without pulling — the escape hatch if a swap ever leaves the tunnel stuck.

Both commands share the same target resolution:

- `--country` replaces the location outright (`--city` may accompany it).
- A lone `--city` keeps the current country.
- Switching `--provider` without a location drops the old country/city.
- When the requested target already matches the running state, nothing is swapped ("Already on").

## Provider details

### Surfshark

WireGuard credentials: Surfshark admin panel → Manual setup → WireGuard.

```bash
vpn up --provider surfshark
```

OpenVPN credentials: admin panel → Manual setup → OpenVPN config (service username/password).

```bash
vpn connect --protocol openvpn   # switch the running tunnel's protocol
```

### ProtonVPN

WireGuard credentials: generate at [account.proton.me/vpn/WireGuard](https://account.proton.me/vpn/WireGuard).

```bash
vpn up --provider protonvpn
```

OpenVPN credentials: [account.proton.me/vpn/OpenVPN](https://account.proton.me/vpn/OpenVPN) → OpenVPN username / password.

```bash
vpn up --provider protonvpn --protocol openvpn
```

`--protocol` defaults to the running one, else wireguard. Credentials for whichever pair you pick are injected from `.env` into the settings document, so provider and protocol switches never require a restart.

## Server selection

`vpn connect --list` lists all servers in an aligned table (provider, protocol, country, city, server) for every provider/protocol pair with valid credentials in `.env`.

`vpn connect` with no arguments opens an interactive picker showing the same columns, with live filtering and keyboard navigation:

- **Filtering** — type to filter, matching any column (accent-insensitive). `Tab`/`Shift-Tab` cycle an active filter *column* (Provider, Protocol, Country, City); while one is active, typing matches only within it and `←`/`→` cycle through that column's distinct values (e.g. `Tab`, `Tab`, `→` picks Surfshark→ProtonVPN; no typing needed). `Esc` exits column mode (or clears the query).
- **Navigation** — `↑`/`↓` or `Ctrl-N`/`Ctrl-P` to move, `PgUp`/`PgDn` for pages, `Home`/`End` for first/last, `Enter` to select, `Ctrl-C`/`Ctrl-Q` to cancel.

The selected row's provider *and* protocol are hot-swapped immediately.

## Runtime selections and drift

Selections made through the control server live at **runtime only** — `.env` stays secrets-only. Consequences:

- A hot-swapped location survives container restarts (`restart: always`) but is lost when the container is recreated (`vpn up --pull`, `vpn up --recreate`) or removed (`vpn down`); recreation reverts to whatever the compose file interpolates from `.env`.
- `vpn status` shows the effective selection and prints a yellow **drift warning** when it differs from the container's configured environment.

## Connection verification

Verification is **leak-first**: a connection counts as up only when the exit IP observed from inside the container differs from the host's bare public IP. The bare IP is fetched host-side once per run (overridable with `VPN_REAL_IP` for testing; when unavailable, leak detection degrades to country heuristics only).

After connecting (and after every swap), the CLI probes the public IP from inside the container and reports one of three verdicts. The probe mirrors gluetun's resilient fetch: four echo services (ipinfo.io, Cloudflare `one.one.one.one/cdn-cgi/trace`, ifconfig.co, ip2location) are queried in parallel from inside the container and the most-agreed result wins, so a rate-limited provider (e.g. ipinfo returning HTTP 429) is absorbed by the rest instead of stalling the retry loop. The verdict notes via yellow when the IP was confirmed by a service other than ipinfo:

- **Green** — real VPN exit in the requested country.
- **Yellow warning** — real VPN exit, but it geolocates elsewhere than requested (common with provider "virtual locations"). You stay connected and the speed test still runs.
- **Red / leak** — traffic still exits via your bare connection (or no IP could be read); the speed test is skipped.

Swaps additionally exclude the previous exit IP from acceptance, so a failed swap that silently keeps routing through the old server is reported as `no reconnect` rather than mistaken for success. IP echo services report ISO 3166-1 alpha-2 codes (`AU`), normalized to full names before comparing. Country is advisory only: it never gates success — the IP change does. `vpn status` and a flag-free `vpn up` on an already-running instance compare against the instance's *running* country, so a drifting exit (e.g. after a virtual-location server was removed) shows as a yellow geo warning instead of a blind green.

## Speed test

`up`, `connect` and `status` run a download speed test after a verified connection (green or yellow verdict). It downloads 25 MB from Cloudflare inside the container — all traffic goes through the VPN tunnel. Skip it per invocation with `--no-speedtest`, or change the size with `vpn status -s 100`. On a leak or unreadable IP, the speed test is skipped with a message.

## Benchmark

`vpn bench` benchmarks locations across **all credentialed providers/protocols** and reports the fastest. It does **not** connect by default — your current location is kept unless you pass `--connect`:

```bash
vpn bench                        # every credentialed provider/protocol; keep current
vpn bench --country Japan        # one country, every provider
vpn bench --provider surfshark   # one provider's countries
vpn bench --protocol openvpn     # one protocol, every provider
vpn bench --connect              # benchmark, then connect to the fastest
vpn bench -c 4                   # bench 4 candidates at once on temp containers
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
- Parallel mode (`-c > 1`) is limited by the credentials your provider permits: if a provider caps simultaneous sessions, some candidates will simply be reported as failures (`no public IP`) while the rest keep benching — it won't abort the run.
- Bench state is applied at runtime only — recreating the container (see [Runtime selections and drift](#runtime-selections-and-drift)) reverts to the env-file selection.

## Instances

vpn 0.2 runs several independent Gluetun containers side by side, each its own *instance*. An instance is identified by a name — its docker container name — and owns:

- a container named exactly `<instance>` (compose project `vpn-<instance>`);
- a control server published on `127.0.0.1:<port>`;
- a lockfile `~/.cache/vpn/locks/<instance>.lock` (swaps/benches serialize per instance only; different instances run concurrently);
- an optional `--env-file` replacing `.env` for that instance;
- a registry record `~/.cache/vpn/instances/<instance>.json` (control port + env file).

Every command requires an instance. Resolution order: `--instance NAME` → `GLUETUN_INSTANCE` env var → interactive choice → error. On an interactive terminal with no `--instance` and no env var, commands that target an instance (`status`, `down`, `connect`, `logs`, `bench`, `dns`, `update`, `up`) ask you to pick one: the sole known instance is used automatically, otherwise a picker lists them by name and state. Non-interactive runs (pipes, scripts) keep the usage error — automation must always name its instance explicitly. `vpn ls` always lists everything and never prompts.

Container names are **exact matches only**: vpn never touches a container other than the one named after the instance, so a shared gluetun owned by another tool is never matched.

### Control port

Each instance publishes the control server on `127.0.0.1:<port>`. **Every** command resolves the port in this order:

1. `--ctl-port HOST_PORT`
2. `GLUETUN_CTL_PORT` (from the instance's env)
3. the registry record (`~/.cache/vpn/instances/<instance>.json`)
4. a registry-less instance's *actually published* host port, read live from Docker — i.e. an imported or shared container vpn never created
5. `8000` — only when the container has no published port

`vpn up` on a brand-new instance auto-allocates a free port in `[8000, 9000]`, persists it to the registry, and writes it into the compose file. A registry-less container keeps its own port: `up` adopts it and every other command targets it, so a shared gluetun created outside vpn stays addressable — `vpn status --json` on such an instance reports its real `control_server.port` instead of a blind `8000`.

`bench -c N` temporary one-off containers never publish host ports.

All control-server traffic (hot-swap `GET/PUT /v1/vpn/settings`, DNS, updater, status) targets the instance's own published port — never a hardcoded `8000`.

### Per-instance env files

Every instance reads its env from `./.env` by default. For a dedicated instance, pass `--env-file <path>`: that file **replaces** `.env` for the instance (compose `--env-file` semantics), while the process environment still fills anything it doesn't set. This lets different instances use different providers/credentials.

### Listing instances

`vpn ls [--json]` enumerates instances from the registry and from containers whose compose project starts with `vpn-`, reporting per-instance state, selection, control-server port, and *consumers* — containers sharing the instance's network (`NetworkMode == container:<instance>`).

```text
$ vpn ls
INSTANCE   STATE    CONTROL  SELECTION                             CONSUMERS
gluetun    running  8000     surfshark/wireguard → Germany         firefox-app, squiz-dev
```

## Configuration

Secrets live in `.env` in the working directory (copy `.env.sample` to get started). Other settings are overridable via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `GLUETUN_INSTANCE` | *(required for scripts)* | Instance name; pass `--instance` or set this. Omitting both asks interactively on a terminal (pick from the known instances); non-interactive runs fail with a usage error. |
| `GLUETUN_CTL_PORT` | unset | Control-server host port for the resolved instance (equivalent to `--ctl-port`) |
| `GLUETUN_CACHE_TTL` | `3600` | Server cache TTL (seconds) |
| `VPN_DEBUG` | unset | Set to enable debug output (same as `--debug`) |

### Credentials

A provider/protocol pair only appears in listings and can only be started when all of its *required* variables are set.

| Provider | Protocol | Variables | Required |
|----------|----------|-----------|----------|
| Surfshark | WireGuard | `SURFSHARK_WIREGUARD_PRIVATE_KEY`, `SURFSHARK_WIREGUARD_ADDRESSES` | private key |
| Surfshark | OpenVPN | `SURFSHARK_OPENVPN_USER`, `SURFSHARK_OPENVPN_PASSWORD` | both |
| ProtonVPN | WireGuard | `PROTONVPN_WIREGUARD_PRIVATE_KEY`, `PROTONVPN_WIREGUARD_ADDRESSES` (always `10.2.0.2/32`) | both |
| ProtonVPN | OpenVPN | `PROTONVPN_OPENVPN_USER`, `PROTONVPN_OPENVPN_PASSWORD` | both |

`HTTP_CONTROL_SERVER_API_KEY` (any random string) is **required** — it authenticates gluetun's HTTP control server, which the CLI exposes on `127.0.0.1:<port>` only (per instance, see [Instances](#instances)). The commands that mutate or select the runtime config (`up`, `connect`, `bench`) refuse to run without it; read-only commands (`status`, `logs`, `ls`, `dns`, `update`, `down`) don't gate on it.

### Set automatically

These are managed by the CLI at container creation time — never define them yourself: `VPN_SERVICE_PROVIDER`, `VPN_TYPE`, `WIREGUARD_PRIVATE_KEY`, `WIREGUARD_ADDRESSES`, `OPENVPN_USER`, `OPENVPN_PASSWORD` (mapped from your provider credentials). A location may be baked via `.env` `SERVER_COUNTRIES`/`SERVER_CITIES` (the compose template interpolates them); after creation, all *runtime* selection changes happen through the control server, and `vpn up` verifies a fresh start — or a `--recreate` — against the baked location when present.

## Consumer API (dockerstrator)

This is the contract `dockerstrator` consumes from `vpn`. Stable schemas — additions only, never removals or renames.

### Instance identity and naming

Instance names follow docker-safe rules (`^[a-zA-Z0-9][a-zA-Z0-9_.-]*$`); anything else is a usage error (exit `2`). The container name is **always** the instance name and is never derived from the compose project. The compose project is pinned to `vpn-<instance>` via `docker compose -p`, independent of the file location.

Resolution order for every command: `--instance NAME` → `GLUETUN_INSTANCE` → interactive choice (TTY only) → error. There is no default instance; a non-interactive run with neither flag is a usage error (exit `2`).

### Exit codes

| Code | Meaning |
|------|---------|
| `0`  | success |
| `1`  | scripted error / VPN failed / leak (verdict in JSON under `--json`) |
| `2`  | usage error |

Other non-zero codes are unspecified. `vpn up` and `vpn connect` return `1` when the connection cannot be verified; `vpn status --json` returns `1` when `leak` is `true` or the control server is unreachable while the container is running/restarting; it returns `0` for any other emitted JSON (probe health failures are reported in `last_error`, never as a leak). `vpn bench` returns `1` (friendly message, no traceback) when the control server becomes unreachable mid-run.

### The calls dockerstrator makes

| Purpose | Command |
|---------|---------|
| Ensure shared gluetun is running (idempotent; verify-only when already up) | `vpn up --instance gluetun` |
| Shared gluetun health probe | `vpn status --json` |
| Capability probe (is vpn 0.2+ implemented?) | `vpn ls --json` (or `vpn --version` for the exact version) |
| Create a dedicated instance (creds from `.env`) | `vpn up --instance <plan>-gluetun --provider P [--protocol T] [--country C] [--city Ci]` |
| Verify a dedicated instance after create | `vpn status --instance <plan>-gluetun --json` |
| Tear down when the plan container is removed | `vpn down --instance <plan>-gluetun` |

dockerstrator rule: if `vpn ls --json` exits non-zero or reports an unknown flag, treat vpn as pre-0.2 and hide the *dedicated* gluetun option (shared-only falls back to plain `vpn up`). Since 0.2.4 every call names its instance explicitly (`--instance` or `GLUETUN_INSTANCE`) — there is no default instance anymore. dockerstrator keeps its own container inventory from `docker ps`; `vpn ls` is used only for instance/control-port/selection state.

`--json` output is deterministic single-line JSON on stdout (no colors, no progress). `--instance` filters `vpn ls` output to one instance.

### `vpn status --json`

```json
{
  "instance": "gluetun",
  "container_name": "gluetun",
  "image": "qmcgaw/gluetun:latest",
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
- `leak` — `true` when the instance is `running` but no exit IP could be read, or the exit IP equals the host's bare public IP.
- `verified` — `true` when the exit verifiably differs from the host's bare IP (or matches the requested country while the bare IP is unknown); `false` otherwise, including when the tunnel is merely stopped or the probe failed.
- `last_error` — human-readable failure detail (e.g. control server unreachable), else `null`.

### `vpn ls --json`

```json
{
  "instances": [
    {
      "instance": "gluetun",
      "container_name": "gluetun",
      "state": "running",
      "selection": { "provider": "surfshark", "protocol": "wireguard", "country": "Japan", "city": "Tokyo" },
      "control_server": { "port": 8000, "enabled": true },
      "consumers": ["firefox-app", "squiz-dev"]
    }
  ]
}
```

- `instances` — one entry per known instance; `state` uses the same values as `status --json`. `selection` and `control_server` are `null` when unknown. `consumers` lists containers sharing the instance's network (`NetworkMode == container:<container_name>`).
