"""Re-roll an instance's exit IP through a random server location.

Rotation keeps the running provider/protocol pair: only the country/city is
randomized, then the tunnel is hot-swapped and its exit IP verified to have
moved. ``--save`` persists the winning location as instance config so a later
create/recreate (``up --pull``/``up --recreate``) keeps it.
"""

from __future__ import annotations

import random
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import click

from epoxy import apply, control, docker, ipinfo, servers
from epoxy.apply import Selection
from epoxy.commands import _common
from epoxy.commands._common import (
    _print_target,
    _require_selection,
    _resolve_targets,
    add_instance_options,
    all_option,
    for_each_instance,
)
from epoxy.config import ROTATE_ATTEMPTS
from epoxy.instance import (
    Instance,
    SavedSelection,
    instance_context,
    read_registry,
    update_saved_selection,
)
from epoxy.rotate import folded_location, location_pool, shuffle_locations
from epoxy.textutil import fold

UsedLocations = dict[tuple[str, str], set[tuple[str, str]]]


@contextmanager
def _use_lock(lock: threading.Lock | None) -> Iterator[None]:
    """Hold the shared ``used`` guard when present (parallel ``--all``)."""
    if lock is None:
        yield
    else:
        with lock:
            yield


def _system_random() -> random.Random:
    """OS-seeded RNG (tests replace this to make picks deterministic)."""
    return random.SystemRandom()


def _pool_for(
    current: Selection,
    country: str | None,
    used: UsedLocations,
) -> list[tuple[str, str | None]]:
    """Random-location candidates for one instance, filtered by its pair."""
    by_provider = servers.listable_servers(servers.get_servers())
    if not any(by_provider.values()):
        raise click.ClickException("No servers found. Is Docker running?")
    rows = next(
        (
            found
            for provider, found in by_provider.items()
            if fold(provider) == fold(current.provider)
        ),
        [],
    )
    pair = (fold(current.provider), fold(current.protocol))
    used_here = used.setdefault(pair, set())
    current_key = folded_location(current.country, current.city)

    def build(exclude: set[tuple[str, str]]) -> list[tuple[str, str | None]]:
        return location_pool(rows, current.provider, current.protocol, country, exclude)

    # Prefer unused locations; when the pool is exhausted, reuse locations but
    # still never re-apply the one the instance is already on.
    pool = build(used_here | {current_key}) or build({current_key})
    if not pool:
        where = f" in {country}" if country else ""
        raise click.ClickException(
            f"No alternate {current.provider}/{current.protocol} servers{where}."
        )
    return pool


def _rotate_one(
    inst: Instance,
    current: Selection,
    country: str | None,
    used: UsedLocations,
    save_location: bool,
    run_speedtest: bool,
    say: Callable[[str], None] = click.echo,
    used_lock: threading.Lock | None = None,
    show_progress: bool = True,
) -> None:
    """Hot-swap one instance until its exit IP changes, then optionally persist."""
    with _use_lock(used_lock):
        pool = shuffle_locations(_pool_for(current, country, used), _system_random())
        attempts = pool[:ROTATE_ATTEMPTS]
        if attempts:
            # Reserve the first pick so parallel instances stay distinct.
            pair = (fold(current.provider), fold(current.protocol))
            used.setdefault(pair, set()).add(folded_location(*attempts[0]))
    prev_ip = ipinfo.current_exit_ip()
    reason = "exit IP did not change"
    for index, (country_name, city) in enumerate(attempts):
        target = Selection(current.provider, current.protocol, country_name, city)
        try:
            apply.apply_location(target)
        except control.ControlError as exc:
            raise click.ClickException(
                f"Could not switch to {_print_target(target)} — {exc.message}"
            ) from exc
        verdict = apply.verify(target, prev_ip)
        if verdict.ok:
            with _use_lock(used_lock):
                used.setdefault((fold(current.provider), fold(current.protocol)), set()).add(
                    folded_location(target.country, target.city)
                )
            say(f"Swapped to {_print_target(target)} — exit {verdict.ip or 'unknown'}.")
            if verdict.geo:
                say(
                    click.style(
                        f"  note: exits via {verdict.geo} (requested {target.country})",
                        fg="yellow",
                    )
                )
            if save_location:
                saved = SavedSelection(
                    target.provider, target.protocol, target.country, target.city
                )
                if not update_saved_selection(inst.name, saved):
                    raise click.ClickException(
                        f"Instance '{inst.name}' has no registry record; could not save."
                    )
                say("Saved as instance config (applied on the next create/recreate).")
            if run_speedtest:
                _common.speed_test(say=say, show_progress=show_progress)
            return
        reason = verdict.reason or reason
        if index < len(attempts) - 1:
            say(f"  {reason}; trying another location.")
    raise click.ClickException(
        f"Could not move the exit IP after {len(attempts)} "
        f"tr{'y' if len(attempts) == 1 else 'ies'} ({reason})."
    )


def _clear_one(inst: Instance) -> None:
    """Drop a persisted selection so recreation goes back to the env file."""
    had_saved = inst.saved_selection is not None
    if not update_saved_selection(inst.name, None):
        raise click.ClickException(
            f"Instance '{inst.name}' has no registry record; nothing to clear."
        )
    click.echo(
        "Saved location cleared (applies on the next create/recreate)."
        if had_saved
        else "No saved location."
    )


@click.command()
@add_instance_options()
@all_option
@click.option("--country", help="Only pick random locations in this country")
@click.option(
    "--save",
    "save_location",
    is_flag=True,
    help="Store the new location as instance config (applied on create/recreate)",
)
@click.option(
    "--clear",
    "clear_saved",
    is_flag=True,
    help="Forget saved locations without rotating",
)
@click.option("--speedtest", is_flag=True, help="Run the speed test after each successful rotation")
def rotate(
    instance: str | None,
    all_instances: bool,
    country: str | None,
    save_location: bool,
    clear_saved: bool,
    speedtest: bool,
) -> None:
    """Change exit IPs by hot-swapping to random server locations."""
    if save_location and clear_saved:
        raise click.UsageError("--save and --clear are mutually exclusive.")
    if clear_saved and country is not None:
        raise click.UsageError("--clear cannot be combined with --country.")
    if clear_saved and speedtest:
        raise click.UsageError("--clear cannot be combined with --speedtest.")

    targets = _resolve_targets(instance, all_instances)
    if clear_saved:
        for_each_instance(
            targets,
            lambda inst, _fan_out: _clear_one(inst),
            all_instances,
            header=True,
            separate=True,
        )
        return

    used: UsedLocations = {}
    used_lock = threading.Lock()

    def rotate_one(
        inst: Instance,
        say: Callable[[str], None] = click.echo,
        lock: threading.Lock | None = None,
        show_progress: bool = True,
    ) -> None:
        if save_location and read_registry(inst.name) is None:
            raise click.ClickException(
                f"Instance '{inst.name}' has no registry record; cannot save a location "
                "for a container epoxy did not create."
            )
        with instance_context(inst):
            _common.require_api_key()
            if not docker.container_running():
                raise click.ClickException(
                    f"Container '{inst.name}' is not running. Use 'epoxy up' first."
                )
            current = _require_selection()
            _rotate_one(
                inst,
                current,
                country,
                used,
                save_location,
                speedtest,
                say=say,
                used_lock=lock,
                show_progress=show_progress,
            )

    def rotate_target(inst: Instance, _fan_out: bool) -> None:
        rotate_one(inst)

    if not all_instances or len(targets) <= 1:
        for_each_instance(targets, rotate_target, all_instances, header=True, separate=True)
        return

    def work(inst: Instance) -> tuple[list[str], BaseException | None]:
        """Rotate one instance, buffering its lines for ordered replay.

        Lines stay per-instance so parallel rotations never interleave; the
        live speedtest bar is suppressed in workers (it cannot draw from
        several threads at once).
        """
        lines: list[str] = []
        try:
            rotate_one(inst, say=lines.append, lock=used_lock, show_progress=False)
        except (Exception, SystemExit) as exc:
            return lines, exc
        return lines, None

    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        futures = [pool.submit(work, inst) for inst in targets]
        try:
            results = [future.result() for future in futures]
        except KeyboardInterrupt:
            pool.shutdown(wait=False, cancel_futures=True)
            raise

    failures = 0
    for index, (inst, (lines, exc)) in enumerate(zip(targets, results, strict=True)):
        if index:
            click.echo()
        click.echo(f"== {inst.name} ==")
        for line in lines:
            click.echo(line)
        if exc is not None:
            _common._record_failure(inst.name, exc)
            failures += 1
    if failures:
        raise SystemExit(1)
