"""Tests for the live download bar: when it draws, and what it leaves behind."""

import io

import pytest
from rich.console import Console
from rich.progress import Progress

from epoxy import progress
from epoxy.commands import _common
from epoxy.speedtest import Result


class FakeProgress:
    """A Progress stand-in that records instead of drawing."""

    def __init__(self, *columns, **kwargs):
        self.kwargs = kwargs
        self.tasks: list[tuple[str, float | None]] = []
        self.updates: list[dict[str, float]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def add_task(self, description, total=None):
        self.tasks.append((description, total))
        return 1

    def update(self, task_id, **kwargs):
        self.updates.append(kwargs)


def _fake_progress(monkeypatch):
    """Swap in a recording Progress; returns every instance handed out."""
    made: list[FakeProgress] = []

    def factory(*args, **kwargs):
        fake = FakeProgress(*args, **kwargs)
        made.append(fake)
        return fake

    monkeypatch.setattr(progress, "Progress", factory)
    return made


def test_stdout_is_tty_follows_stdout(monkeypatch):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    assert progress.stdout_is_tty() is True
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    assert progress.stdout_is_tty() is False


def test_bar_draws_nothing_off_a_terminal(monkeypatch, capsys):
    """Piped output and CI logs keep getting plain lines, not escape codes."""
    monkeypatch.setattr(progress, "stdout_is_tty", lambda: False)
    with progress.download_bar(25, 25_000_000) as report:
        for done in range(0, 25_000_001, 5_000_000):
            report(done, 25_000_000)  # still callable: the download is not disturbed
    assert capsys.readouterr().out == ""


def test_bar_reports_running_totals(monkeypatch):
    made = _fake_progress(monkeypatch)
    monkeypatch.setattr(progress, "stdout_is_tty", lambda: True)
    with progress.download_bar(25, 25_000_000) as report:
        report(5_000_000, 25_000_000)
        report(25_000_000, 25_000_000)
    assert made[0].tasks == [("Downloading 25 MB", 25_000_000)]
    assert made[0].updates == [
        {"completed": 5_000_000, "total": 25_000_000},
        {"completed": 25_000_000, "total": 25_000_000},
    ]


def test_the_bar_is_transient(monkeypatch):
    """Erasure is what keeps the result line the only lasting record."""
    made = _fake_progress(monkeypatch)
    monkeypatch.setattr(progress, "stdout_is_tty", lambda: True)
    with progress.download_bar(25, 25_000_000):
        pass
    assert made[0].kwargs["transient"] is True


def test_the_real_bar_draws_a_filling_bar(monkeypatch):
    """The real columns, against a console that pretends to be a terminal."""
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, width=100)
    monkeypatch.setattr(progress, "stdout_is_tty", lambda: True)
    monkeypatch.setattr(progress, "Progress", lambda *a, **kw: Progress(*a, console=console, **kw))
    with progress.download_bar(25, 25_000_000) as report:
        report(5_000_000, 25_000_000)
        report(25_000_000, 25_000_000)
    drawn = buf.getvalue()
    assert "Downloading 25 MB" in drawn
    assert "100%" in drawn  # the columns include a percentage, not just sizes
    assert "25.0/25.0 MB" in drawn


@pytest.fixture()
def wired(monkeypatch):
    """finish_connection with a verified connection and a drawing bar."""
    seen: dict[str, object] = {}

    def measure(size, **kwargs):
        seen["on_progress"] = kwargs.get("on_progress")
        return Result(mbits=20.0, seconds=10.0, mbytes=float(size))

    monkeypatch.setattr("epoxy.ipinfo.print_ip_status", lambda **kwargs: True)
    monkeypatch.setattr("epoxy.speedtest.measure", measure)
    monkeypatch.setattr(progress, "stdout_is_tty", lambda: True)
    _fake_progress(monkeypatch)
    return seen


def test_the_bar_adds_nothing_to_the_result(wired, capsys):
    """The documented single-instance output, byte for byte."""
    assert _common.finish_connection(size=25) is True
    assert capsys.readouterr().out == "Running speed test...\n↓ 20.0 Mbit/s (25 MB in 10.0s)\n"
    assert callable(wired["on_progress"])  # the download really is watched


def test_a_failed_download_still_reports_itself(wired, capsys, monkeypatch):
    monkeypatch.setattr("epoxy.speedtest.measure", lambda size, **kwargs: None)
    _common.finish_connection(size=25)
    assert capsys.readouterr().out == "Running speed test...\nSpeed test failed.\n"


def test_the_same_output_off_a_terminal(wired, capsys, monkeypatch):
    """A redirected stdout must produce byte-identical output to a terminal."""
    monkeypatch.setattr(progress, "stdout_is_tty", lambda: False)
    _common.finish_connection(size=25)
    assert capsys.readouterr().out == "Running speed test...\n↓ 20.0 Mbit/s (25 MB in 10.0s)\n"
