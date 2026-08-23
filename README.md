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
| `vpn restart` | Restart with current config |
| `vpn ip` | Show public VPN IP |
| `vpn status` | Container state + public IP |
| `vpn logs` | Show container logs (`-f` to follow, `-n` for line count) |
| `vpn update` | Pull latest gluetun image + recreate |
| `vpn speedtest` | Measure download speed through the VPN (`--size` MB, default 25) |
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

`--protocol` defaults to `wireguard`. `vpn update` preserves the protocol of the running container.

## Server selection

`vpn servers` lists all servers in an aligned table (provider, protocol, country, city, server) for every provider/protocol pair with valid credentials in `.env`.

`vpn server` opens an interactive picker showing the same columns, with live filtering (accent-insensitive) and keyboard navigation (↑/↓ or Ctrl-N/P to move, PgUp/PgDn for pages, Home/End for first/last, type to filter — the filter matches any column including provider and protocol, Enter to select, Ctrl-C/Q to cancel). The selected row's provider *and* protocol are automatically used when restarting the container.

## Connection verification

After connecting, the CLI probes the public IP from inside the container (`wget https://ipinfo.io`, time-bounded) and compares the reported country with the selected server's country — shown green on match, red on mismatch. IP echo services report ISO 3166-1 alpha-2 codes (`AU`), which are normalized to full names before comparing, so they match gluetun's server lists.

## Speed test

`up`, `update` and `server` run a download speed test after a verified connection (green `Location:`). It downloads 25 MB from Cloudflare inside the container — all traffic goes through the VPN tunnel. Skip it per invocation with `--no-speedtest`, or change the size with `vpn speedtest --size 100`. When the connection isn't verified, the speed test is skipped with a message.

## Configuration

All settings are overridable via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `GLUETUN_CONTAINER` | `gluetun` | Container name |
| `GLUETUN_COMPOSE_FILE` | bundled `vpn.yml` | Path to compose file (a `./vpn.yml` in the working directory takes precedence) |
| `GLUETUN_CACHE_TTL` | `3600` | Server cache TTL (seconds) |
