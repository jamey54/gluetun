"""Live progress for long-running downloads.

Kept apart from the commands so the display can be exercised on its own, and
apart from speedtest so that module stays about throughput and not about pixels.
"""

import sys
from collections.abc import Iterator
from contextlib import contextmanager

from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from epoxy.speedtest import ProgressFn


def _discard(downloaded: int, total: int) -> None:
    """Progress callback for when nothing should be drawn."""


def stdout_is_tty() -> bool:
    """True when a live display is welcome: only a human at a terminal wants one."""
    return sys.stdout.isatty()


@contextmanager
def download_bar(size_mb: int, total_bytes: int) -> Iterator[ProgressFn]:
    """Yield a callback that drives a live bar for a download of total_bytes.

    Nothing is drawn when stdout is not a terminal, so piped output, CI logs and
    ``--json`` consumers keep seeing the plain lines they saw before — the
    callback still runs, it just reports into the void.

    The bar is transient: it is erased when the block ends, leaving the result
    line as the only lasting record of the download.
    """
    if not stdout_is_tty():
        yield _discard
        return
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        BarColumn(bar_width=28),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        transient=True,
    ) as progress:
        task = progress.add_task(f"Downloading {size_mb} MB", total=total_bytes)

        def report(downloaded: int, total: int) -> None:
            progress.update(task, completed=downloaded, total=total)

        yield report
