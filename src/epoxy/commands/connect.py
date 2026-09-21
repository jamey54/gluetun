"""Hot-swap to another server without restarting; no arguments opens the picker."""

import click

from epoxy import docker, ipinfo, picker, servers
from epoxy.commands import _common
from epoxy.commands._common import (
    PROTOCOL,
    _apply_request,
    _print_target,
    _require_selection,
    _resolve_for_command,
    add_instance_options,
)
from epoxy.instance import current_instance, instance_context


@click.command()
@add_instance_options()
@click.option("--provider", help="VPN provider")
@click.option("--protocol", type=PROTOCOL, default=None, help="VPN protocol")
@click.option("--country", help="Country to connect to")
@click.option("--city", help="City within the country")
@click.option("--list", "list_servers", is_flag=True, help="List available servers and exit")
@click.option("--no-speedtest", is_flag=True, help="Skip the post-connect speed test")
def connect(
    instance: str | None,
    provider: str | None,
    protocol: str | None,
    country: str | None,
    city: str | None,
    list_servers: bool,
    no_speedtest: bool,
) -> None:
    """Hot-swap to another server without restarting; no arguments opens the picker."""
    if list_servers:
        by_provider = servers.listable_servers(servers.get_servers())
        if not any(by_provider.values()):
            raise click.ClickException("No servers found. Is Docker running?")
        servers.print_servers_table(by_provider)
        return
    inst = _resolve_for_command(instance)
    with instance_context(inst):
        _common.require_api_key()
        if not docker.container_running():
            raise click.ClickException(
                f"Container '{current_instance().container}' is not running. Use 'epoxy up' first."
            )
        current = _require_selection()

        if not any(v is not None for v in (provider, protocol, country, city)):
            by_provider = servers.listable_servers(servers.get_servers())
            if not any(by_provider.values()):
                raise click.ClickException("No servers found. Is Docker running?")
            selection = picker.select_server(by_provider)
            if not selection:
                raise click.ClickException("No selection.")
            picked = servers.parse_server_selection(selection)
            picked_provider, picked_protocol, country, city = picked
            provider, protocol = picked_provider, picked_protocol

        prev_ip = ipinfo.current_exit_ip()
        target, swapped = _apply_request(provider, protocol, country, city, current)
        click.echo(f"{'Swapped to' if swapped else 'Already on'} {_print_target(target)}.")
        verified = _common.finish_connection(
            expected_country=target.country,
            run_speedtest=not no_speedtest,
            # Only exclude when we actually moved; when already on target the
            # tunnel still exits via prev_ip itself, which must count as success.
            exclude_ips={prev_ip} if (prev_ip and swapped) else None,
        )
    if not verified:
        raise SystemExit(1)
