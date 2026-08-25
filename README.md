# vpn

Small Python CLI for managing a [Gluetun](https://github.com/qdm12/gluetun) VPN container via docker compose.

## Supported providers

| Provider  | WireGuard | OpenVPN |
|-----------|-----------|---------|
| Surfshark | ✓         | ✓       |
| ProtonVPN | ✓         | ✓       |

All providers can be active simultaneously — their servers appear side by side in `vpn servers` and the picker, tagged with their protocol. A provider's protocol is listed only when its credentials are present in `.env`.

## Requirements

- Python 3.10+
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

Each provider has its own prefixed credentials in `.env`:

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
| `vpn up --provider <name>` | Start the VPN container |
| `vpn down` | Stop the VPN container |
| `vpn restart` | Restart with current config (runs speed test) |
| `vpn ip` | Show public VPN IP (no speed test) |
| `vpn status` | Container state + public IP + speed test |
| `vpn logs` | Show container logs (`-f` to follow, `-n` for line count) |
| `vpn update` | Pull latest gluetun image + recreate |
| `vpn speedtest` | Measure download speed through the VPN (`--size` MB, default 25) |
| `vpn bench` | Benchmark locations and connect to the fastest |
| `vpn server` | Interactive picker — pick location, restart |
| `vpn servers` | List available servers (all active providers) |

## Provider details

### Surfshark

WireGuard credentials: Surfshark admin panel → Manual setup → WireGuard.

```bash
vpn up --provider surfshark
```

OpenVPN credentials: admin panel → Manual setup → OpenVPN config (service username/password).

```bash
vpn up --provider surfshark --protocol openvpn
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

`--protocol` defaults to the running container's protocol (`wireguard` on a fresh install). `vpn up` and `vpn update` preserve the running container's country/city selection unless you pick a new one via `vpn server`.

## Server selection

`vpn servers` lists all servers in an aligned table (provider, protocol, country, city, server) for every provider/protocol pair with valid credentials in `.env`.

`vpn server` opens an interactive picker showing the same columns, with live filtering (accent-insensitive) and keyboard navigation (↑/↓ or Ctrl-N/P to move, PgUp/PgDn for pages, Home/End for first/last, type to filter — the filter matches any column including provider and protocol, Enter to select, Ctrl-C/Q to cancel). The selected row's provider *and* protocol are automatically used when restarting the container.

## Connection verification

Verification is **leak-first**: a connection counts as up only when the exit IP observed from inside the container differs from the host's bare public IP. The bare IP is fetched host-side once per run (overridable with `VPN_REAL_IP` for testing; when unavailable, leak detection degrades to country heuristics only).

After connecting, the CLI probes the public IP from inside the container (`wget https://ipinfo.io`, time-bounded) and reports one of three verdicts:

- **Green** — real VPN exit in the requested country.
- **Yellow warning** — real VPN exit, but it geolocates elsewhere than requested (common with provider "virtual locations"). You stay connected and the speed test still runs.
- **Red / leak** — traffic still exits via your bare connection (or no IP could be read); the speed test is skipped.

IP echo services report ISO 3166-1 alpha-2 codes (`AU`), which are normalized to full names before comparing, so they match gluetun's server lists. Country is advisory only: it never gates success — the IP change does.

## Speed test

`up`, `update`, `server`, `status` and `restart` run a download speed test after a verified connection (green or yellow `Location:`). It downloads 25 MB from Cloudflare inside the container — all traffic goes through the VPN tunnel. Skip it per invocation with `--no-speedtest`, or change the size with `vpn speedtest --size 100`. On a leak or unreadable IP, the speed test is skipped with a message.

## Benchmark

`vpn bench` finds and connects to the fastest location:

```bash
vpn bench                        # running provider/protocol, all its countries
vpn bench --country Japan        # one country
vpn bench --all                  # every credentialed provider/protocol
vpn bench --no-connect           # report results, keep the current location
```

How it runs:

1. **Latency prescreen** — parallel TCP-connect probes (port 443, host-side) rank every candidate location; unreachable ones sort last.
2. **Screening** — the top `--top` (default 12) locations each get hot-swapped in place, proven by IP change (exit IP must differ from your bare IP *and* the previous exit — leaks and failed swaps are marked `leak` / `no reconnect`), then tested with a `--scan-size` MB (default 10) download. Exits that geolocate outside the requested country are flagged `geo: <country>` but still tested.
3. **Finals** — the best 3 are re-tested with the full `-s/--size` MB (default 25) download.
4. **Winner** — connected automatically after a final re-check of the exit IP. With `--no-connect` (or Ctrl-C at any point) the pre-bench settings are restored instead.

Each test hot-swaps through gluetun's control server (`GET/PUT /v1/vpn/settings`) — no container recreation, single-digit-second switches. On older images without that route it falls back to recreating the container per location. Cross-provider benches work because the correct credentials for each candidate pair are injected from `.env` into the settings document.

Notes:

- Candidates default to the running provider/protocol; use `--provider`, `--protocol`, or `--all` to widen.
- Bench state is applied at runtime only — a later `docker compose up -d --force-recreate` (e.g. `vpn update`) reverts to the env-file selection.

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

`HTTP_CONTROL_SERVER_API_KEY` (any random string) is **required** — it authenticates gluetun's HTTP control server, which the CLI exposes on `127.0.0.1:8000` only. `up`, `update` and `server` refuse to run without it.

### Set automatically

These are managed by the CLI — never define them yourself: `VPN_SERVICE_PROVIDER`, `VPN_TYPE`, `WIREGUARD_PRIVATE_KEY`, `WIREGUARD_ADDRESSES`, `OPENVPN_USER`, `OPENVPN_PASSWORD` (mapped from your provider credentials), and `SERVER_COUNTRIES` / `SERVER_CITIES` (written by `vpn server`; preserved across `up` and `update`).
