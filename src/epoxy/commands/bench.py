"""Benchmark locations and report the fastest."""

import click

from epoxy import control, docker, servers
from epoxy.bench import (
    DEFAULT_FINAL_SIZE_MB,
    DEFAULT_TOP,
    build_candidates,
    print_report,
    run_bench,
)
from epoxy.commands import _common
from epoxy.commands._common import PROTOCOL, _resolve_for_command, add_instance_options
from epoxy.config import DEFAULT_SCAN_SIZE_MB, DEFAULT_TEST_CONCURRENCY
from epoxy.instance import current_instance, instance_context


@click.command()
@add_instance_options()
@click.option("--provider", help="Only bench this provider (default: all credentialed)")
@click.option(
    "--protocol",
    type=PROTOCOL,
    default=None,
    help="Only bench this protocol (default: all credentialed)",
)
@click.option("--country", help="Only bench this country")
@click.option(
    "-n",
    "--max-candidates",
    type=click.IntRange(min=0),
    default=0,
    help="Cap on locations entering the latency stage (default: no cap)",
)
@click.option(
    "--top",
    type=click.IntRange(min=1),
    default=DEFAULT_TOP,
    show_default=True,
    help="Locations that get a screening download after latency ranking",
)
@click.option(
    "--scan-size",
    type=click.IntRange(min=1),
    default=DEFAULT_SCAN_SIZE_MB,
    show_default=True,
    help="Screening download size (MB)",
)
@click.option(
    "-s",
    "--size",
    type=click.IntRange(min=1),
    default=DEFAULT_FINAL_SIZE_MB,
    show_default=True,
    help="Finals download size (MB)",
)
@click.option(
    "-c",
    "--concurrency",
    type=click.IntRange(min=1),
    default=DEFAULT_TEST_CONCURRENCY,
    show_default=True,
    help="Candidates tested in parallel on temporary containers (1 = test on the running one)",
)
@click.option(
    "--connect",
    is_flag=True,
    help="Connect to the fastest location after benchmarking (default: keep the current location)",
)
def bench(
    instance: str | None,
    provider: str | None,
    protocol: str | None,
    country: str | None,
    max_candidates: int,
    top: int,
    scan_size: int,
    size: int,
    concurrency: int,
    connect: bool,
) -> None:
    """Benchmark locations and report the fastest.

    By default every credentialed provider/protocol is benched; narrow with
    --provider/--protocol/--country, or cap scale with -n/--max-candidates.
    The current location is kept unless --connect is passed.
    """
    with instance_context(_resolve_for_command(instance)):
        _common.require_api_key()
        if not docker.container_running():
            raise click.ClickException(
                f"Container '{current_instance().container}' is not running."
            )
        by_provider = servers.listable_servers(servers.get_servers())
        if not any(by_provider.values()):
            raise click.ClickException("No servers found. Is Docker running?")

        try:
            control.get_settings()
        except control.ControlError as exc:
            raise click.ClickException(f"Cannot reach the control server: {exc.message}") from None

        candidates = build_candidates(by_provider, provider, protocol, country)
        if not candidates:
            raise click.ClickException("No matching locations for the given filters.")

        try:
            report = run_bench(
                candidates,
                top=top,
                limit=max_candidates,
                scan_size_mb=scan_size,
                final_size_mb=size,
                concurrency=concurrency,
                connect_winner=connect,
            )
        except KeyboardInterrupt:
            raise SystemExit(130) from None
        except control.ControlError as exc:
            raise click.ClickException(f"Cannot reach the control server: {exc.message}") from None

        print_report(report)
        if report.action:
            click.echo(report.action)
