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
| `vpn up [--provider --protocol --country --city] [--pull] [--recreate]` | Start the VPN; apply any requested location via hot-swap |
| `vpn connect [--provider --protocol --country --city] [--list]` | Hot-swap to another server; no arguments opens the picker |
| `vpn status [-s SIZE] [--no-speedtest]` | Container state, effective selection, public IP, speed test |
| `vpn down` | Stop the VPN container |
| `vpn logs [-f] [-n N]` | Show container logs |
| `vpn bench [--connect]` | Benchmark locations and report the fastest (keeps current unless `--connect`) |

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

After connecting (and after every swap), the CLI probes the public IP from inside the container (`wget https://ipinfo.io`, time-bounded) and reports one of three verdicts:

- **Green** — real VPN exit in the requested country.
- **Yellow warning** — real VPN exit, but it geolocates elsewhere than requested (common with provider "virtual locations"). You stay connected and the speed test still runs.
- **Red / leak** — traffic still exits via your bare connection (or no IP could be read); the speed test is skipped.

Swaps additionally exclude the previous exit IP from acceptance, so a failed swap that silently keeps routing through the old server is reported as `no reconnect` rather than mistaken for success. IP echo services report ISO 3166-1 alpha-2 codes (`AU`), normalized to full names before comparing. Country is advisory only: it never gates success — the IP change does.

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
4. **Result** — without `--connect` the pre-bench settings are restored and the table reports the winner. With `--connect` the winner is adopted after a final re-check of the exit IP. In either case Ctrl-C at any point restores the pre-bench settings.

The default `-c/--concurrency 1` hot-swaps the running container for every test. Raise it (e.g. `-c 4`) to run the screening and final stages as batches of concurrent tests, each candidate on its own **temporary one-off container**; your live connection is then left untouched until the winner is connected. All temporary containers are removed after each batch, and aborted runs tear them down too.

All swaps go through the same locked runtime engine as `connect`: concurrent CLI invocations (e.g. a bench while someone picks a server) serialize on a lockfile instead of clobbering each other's settings.

Notes:

- Candidates default to every credentialed provider/protocol; use `--provider`, `--protocol`, or `--country` to narrow, or `-n/--max-candidates` to cap the ones entering the latency stage.
- Parallel mode (`-c > 1`) is limited by the credentials your provider permits: if a provider caps simultaneous sessions, some candidates will simply be reported as failures (`no public IP`) while the rest keep benching — it won't abort the run.
- Bench state is applied at runtime only — recreating the container (see [Runtime selections and drift](#runtime-selections-and-drift)) reverts to the env-file selection.

## Configuration

Secrets live in `.env` (copy `.env.sample`; located next to your compose file). Other settings are overridable via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `GLUETUN_CONTAINER` | `gluetun` | Container name |
| `GLUETUN_COMPOSE_FILE` | bundled `vpn.yml` | Path to compose file (a `./vpn.yml` in the working directory takes precedence) |
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

`HTTP_CONTROL_SERVER_API_KEY` (any random string) is **required** — it authenticates gluetun's HTTP control server, which the CLI exposes on `127.0.0.1:8000` only. Every command except `down` and `logs` refuses to run without it.

### Set automatically

These are managed by the CLI at container creation time — never define them yourself: `VPN_SERVICE_PROVIDER`, `VPN_TYPE`, `WIREGUARD_PRIVATE_KEY`, `WIREGUARD_ADDRESSES`, `OPENVPN_USER`, `OPENVPN_PASSWORD` (mapped from your provider credentials). Location is *not* baked into env vars: after creation, all selection changes happen through the control server.
