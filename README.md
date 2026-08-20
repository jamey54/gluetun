# vpn

Single-file CLI for managing a [Gluetun](https://github.com/qdm12/gluetun) VPN container.

## Supported providers

- **Surfshark** — WireGuard
- **ProtonVPN** — WireGuard

Both providers can be active simultaneously (their servers appear side by side in `vpn servers`).

## Requirements

- Python 3.8+
- [click](https://click.palletsprojects.com/) — `pip install click`
- [fzf](https://github.com/junegunn/fzf) — `sudo apt install fzf`

## Install

```bash
pip install click
sudo apt install fzf
chmod +x vpn.py
```

Optional — symlink so `vpn` is available everywhere:

```bash
ln -s $(pwd)/vpn.py /usr/local/bin/vpn
```

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
| `vpn server` | Interactive fzf search — pick location, restart |
| `vpn servers` | List available servers (all active providers) |

## Provider details

### Surfshark

WireGuard credentials: Surfshark admin panel → Manual setup → WireGuard.

```bash
vpn up --provider surfshark
```

### ProtonVPN

WireGuard credentials: generate at [account.proton.me/vpn/WireGuard](https://account.proton.me/vpn/WireGuard).

```bash
vpn up --provider protonvpn
```

## Server selection

`vpn servers` lists servers for all providers with valid credentials in `.env`.

`vpn server` opens an fzf picker showing servers from all active providers. The selected provider is automatically used when restarting the container.

## Configuration

All settings are overridable via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `GLUETUN_CONTAINER` | `gluetun` | Container name |
| `GLUETUN_COMPOSE_FILE` | `./vpn.yml` | Path to compose file |
| `GLUETUN_CACHE_TTL` | `3600` | Server cache TTL (seconds) |
