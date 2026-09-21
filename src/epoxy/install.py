"""Shell-completion installer: wire `epoxy` tab completion into shell rc files.

Completion itself is Click's native ``*_source`` mode — this module only
delivers the one-line ``eval`` snippet to the right rc file inside an
idempotent marker block, and warns when ``epoxy`` is not on ``PATH`` yet.
"""

import os
import shutil
from pathlib import Path

import click

START_MARKER = "# >>> epoxy completion >>>"
END_MARKER = "# <<< epoxy completion <<<"

_SNIPPETS = {
    "bash": 'eval "$(_EPOXY_COMPLETE=bash_source epoxy)"',
    "zsh": 'eval "$(_EPOXY_COMPLETE=zsh_source epoxy)"',
    "fish": "eval (env _EPOXY_COMPLETE=fish_source epoxy)",
}

_DEFAULT_RCS = {
    "bash": ".bashrc",
    "zsh": ".zshrc",
    "fish": ".config/fish/config.fish",
}


def detect_shell(explicit: str) -> str:
    """Resolve the target shell: an explicit choice, or the basename of $SHELL."""
    if explicit != "auto":
        return explicit
    shell = os.path.basename(os.getenv("SHELL", ""))
    if shell in _SNIPPETS:
        return shell
    raise click.UsageError(
        f"Cannot detect shell from $SHELL ({os.getenv('SHELL', '')!r}). Pass --shell bash|zsh|fish."
    )


def snippet_for(shell: str) -> str:
    """The one-line eval snippet Click's completion needs for this shell."""
    return _SNIPPETS[shell]


def default_rc(shell: str) -> Path:
    """The rc file completion is appended to when --rc-file is omitted."""
    return Path.home() / _DEFAULT_RCS[shell]


def completion_block(shell: str) -> str:
    """The full marker-wrapped block written to (or printed for) the rc file."""
    return f"{START_MARKER}\n{snippet_for(shell)}\n{END_MARKER}\n"


def path_hint() -> str | None:
    """Install hint when `epoxy` is not on PATH; None when it resolves."""
    if shutil.which("epoxy") is None:
        return "Note: `epoxy` is not on PATH — run `pip install .` first, then reload your shell."
    return None


def upsert_block(rc: Path, block: str, force: bool) -> str:
    """Merge the marker block into an rc file; return written|unchanged|updated.

    Never touches content outside the markers. A differing block already in
    place is only replaced with ``force`` — otherwise this is a usage error,
    so hand edits inside the markers are never silently clobbered.
    """
    existing = rc.read_text() if rc.exists() else ""
    if START_MARKER not in existing:
        rc.parent.mkdir(parents=True, exist_ok=True)
        if not existing:
            rc.write_text(block)
        else:
            gap = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
            rc.write_text(f"{existing}{gap}{block}")
        return "written"
    if block in existing:
        return "unchanged"
    if not force:
        raise click.UsageError(
            f"{rc} already has an epoxy completion block with different content. "
            "Re-run with --force to replace it."
        )
    start = existing.index(START_MARKER)
    end_pos = existing.find(END_MARKER, start)
    end = end_pos + len(END_MARKER) if end_pos != -1 else len(existing)
    updated = existing[:start] + block.rstrip("\n") + existing[end:]
    rc.write_text(updated if updated.endswith("\n") else updated + "\n")
    return "updated"
