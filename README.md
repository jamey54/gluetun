# vpn

Single-file CLI for managing a [Gluetun](https://github.com/qdm12/gluetun) VPN container.

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

## Commands

| Command | Description |
|---------|-------------|
| `vpn up` | Start the VPN container |
| `vpn down` | Stop the VPN container |
| `vpn restart` | Restart with current config |
| `vpn ip` | Show public VPN IP |
| `vpn status` | Container state + public IP |
| `vpn logs` | Show container logs (`-f` to follow, `-n` for line count) |
| `vpn update` | Pull latest gluetun image + recreate |
| `vpn server` | Interactive fzf search — pick location, restart |
| `vpn servers` | List available servers |

## Configuration

All settings are overridable via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `GLUETUN_CONTAINER` | `gluetun` | Container name |
| `GLUETUN_COMPOSE_FILE` | `./vpn.yml` | Path to compose file |
| `GLUETUN_PROVIDER` | `surfshark` | VPN provider |
| `GLUETUN_CACHE_TTL` | `3600` | Server cache TTL (seconds) |

## vpn.yml

Secrets are stored in `.env` (not committed — see `.gitignore`).

```bash
cp .env.sample .env
# edit .env with your Surfshark WireGuard credentials
```

Required values in `.env`:

- `WIREGUARD_PRIVATE_KEY`
- `WIREGUARD_ADDRESSES`
- `HTTP_CONTROL_SERVER_API_KEY`

Location is set via `SERVER_COUNTRIES` / `SERVER_CITIES` in `vpn.yml` — use `vpn server` to change interactively.
