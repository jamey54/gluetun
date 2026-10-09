"""List available VPN servers (no instance required)."""

import json

import click

from epoxy import providers, servers
from epoxy.commands._common import PROTOCOL
from epoxy.config import DEFAULT_IMAGE, DEFAULT_PROTOCOL, IMAGE_ENV_VAR
from epoxy.instance import build_env


@click.command("servers")
@click.option("--provider", default=None, help="Only list this provider")
@click.option(
    "--protocol",
    type=PROTOCOL,
    default=None,
    help="Only list this protocol",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Machine-readable list (single-line JSON)",
)
def servers_cmd(provider: str | None, protocol: str | None, json_output: bool) -> None:
    """List servers for every credentialed provider/protocol pair.

    Needs no instance and no running container, so it works before anything
    is created: credentials and the image come from the process environment
    and ``.env``, the same sources every instance falls back to.
    """
    env = build_env(None)
    image = env.get(IMAGE_ENV_VAR) or DEFAULT_IMAGE
    listing = servers.listable_servers(
        servers.get_servers(image=image),
        providers.get_active_providers_in(env),
    )
    if not any(listing.values()):
        raise click.ClickException("No servers found. Is Docker running?")
    if provider is not None:
        name = provider.lower()
        if name not in providers.PROVIDERS:
            valid = ", ".join(sorted(providers.PROVIDERS))
            raise click.ClickException(f"Unknown provider '{provider}'. Available: {valid}")
        listing = {name: listing.get(name, [])}
    if protocol is not None:
        wanted = protocol.lower()
        listing = {
            prov: [s for s in rows if s.get("vpn", DEFAULT_PROTOCOL) == wanted]
            for prov, rows in listing.items()
        }
        listing = {prov: rows for prov, rows in listing.items() if rows}
    if not any(listing.values()):
        raise click.ClickException("No matching servers for the given filters.")
    if json_output:
        rows = [
            {
                "provider": prov,
                "protocol": proto,
                "country": country,
                "city": city,
                "hostname": hostname,
            }
            for prov, proto, country, city, hostname in servers.sorted_server_rows(listing)
        ]
        click.echo(json.dumps({"servers": rows}))
        return
    servers.print_servers_table(listing)
