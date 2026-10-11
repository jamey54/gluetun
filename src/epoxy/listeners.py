"""Host listener lookup: name the process holding a TCP port.

Used to make port-conflict errors precise (``held by process 1234
(docker-proxy)``). Pure stdlib over Linux ``/proc``; anything unreadable or
missing degrades to None and the caller keeps its generic message. All
readers take an explicit proc root so tests feed fixture files instead of the
live ``/proc``.
"""

import os
from pathlib import Path

#: Default proc filesystem (overridable per call for tests).
PROC_ROOT = Path("/proc")

#: /proc/net/tcp{,6} state for a listening socket.
_LISTEN_STATE = "0A"


def _decode_ipv4(hex_ip: str) -> str | None:
    """/proc little-endian hex quad to dotted IPv4; None when malformed."""
    if len(hex_ip) != 8:
        return None
    try:
        raw = bytes.fromhex(hex_ip)
    except ValueError:
        return None
    return ".".join(str(byte) for byte in raw[::-1])


def _decode_ipv6_groups(hex_ip: str) -> tuple[int, ...] | None:
    """32-hex-digit address to 8 big-endian groups; None when malformed."""
    if len(hex_ip) != 32:
        return None
    try:
        raw = bytes.fromhex(hex_ip)
    except ValueError:
        return None
    return tuple(int.from_bytes(raw[i : i + 2], "big") for i in range(0, 16, 2))


def _compress_ipv6(groups: tuple[int, ...]) -> str:
    """Shortest ``::`` form of 8 groups (matches spellings like ``::1``)."""
    best_start = 0
    best_len = 0
    run_start = 0
    run_len = 0
    for i, group in enumerate(groups):
        if group == 0:
            if run_len == 0:
                run_start = i
            run_len += 1
            if run_len > best_len:
                best_start, best_len = run_start, run_len
        else:
            run_len = 0
    if best_len < 2:
        return ":".join(f"{group:x}" for group in groups)
    head = ":".join(f"{group:x}" for group in groups[:best_start])
    tail = ":".join(f"{group:x}" for group in groups[best_start + best_len :])
    if not head:
        return f"::{tail}" if tail else "::"
    if not tail:
        return f"{head}::"
    return f"{head}::{tail}"


def _addr_conflicts(addr_hex: str, want: str) -> bool:
    """True when a listener on ``addr_hex`` blocks binding ``want``.

    A wildcard listener conflicts with any address; otherwise the addresses
    must match (IPv4-mapped IPv6 folds onto its IPv4 form). Unparseable
    addresses never match: the port-only fallback covers them.
    """
    groups = _decode_ipv6_groups(addr_hex)
    if groups is not None:
        if all(group == 0 for group in groups):
            return True
        if _compress_ipv6(groups) == want:
            return True
        if groups[:6] == (0, 0, 0, 0, 0, 0xFFFF):
            tail = (groups[6] << 16) | groups[7]
            mapped = ".".join(str((tail >> shift) & 0xFF) for shift in (24, 16, 8, 0))
            return mapped == want
        return False
    dotted = _decode_ipv4(addr_hex)
    return dotted is not None and dotted in ("0.0.0.0", want)


def _listening_inodes(port: int, want_ip: str, proc_root: Path) -> tuple[set[int], set[int]]:
    """(address-matching, port-only) LISTEN socket inodes for ``port``.

    The caller prefers address-matching inodes and falls back to port-only
    ones, so an exotic spelling never yields "unknown" while an exact match
    wins when several listeners share the port.
    """
    matching: set[int] = set()
    port_only: set[int] = set()
    for table in ("tcp", "tcp6"):
        try:
            lines = (proc_root / "net" / table).read_text().splitlines()
        except OSError:
            continue
        for line in lines[1:]:
            parts = line.split()
            if len(parts) < 10 or parts[3] != _LISTEN_STATE:
                continue
            addr_hex, _, port_hex = parts[1].rpartition(":")
            try:
                found = int(port_hex, 16)
            except ValueError:
                continue
            if found != port or not parts[9].isdigit():
                continue
            inode = int(parts[9])
            if not inode:
                continue
            port_only.add(inode)
            if _addr_conflicts(addr_hex, want_ip):
                matching.add(inode)
    return matching, port_only


def holder_of(
    port: int, bind_ip: str, proc_root: Path = PROC_ROOT
) -> tuple[int, str | None] | None:
    """(pid, comm) holding a conflicting listener; None when unknown.

    Pids scan numerically for determinism; vanishing processes and unreadable
    descriptors are skipped, never errors.
    """
    matching, port_only = _listening_inodes(port, bind_ip, proc_root)
    inodes = matching or port_only
    if not inodes:
        return None
    try:
        pids = sorted(int(entry.name) for entry in proc_root.iterdir() if entry.name.isdigit())
    except OSError:
        return None
    for pid in pids:
        try:
            fds = list((proc_root / str(pid) / "fd").iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if any(target == f"socket:[{inode}]" for inode in inodes):
                try:
                    comm = (proc_root / str(pid) / "comm").read_text().strip() or None
                except OSError:
                    comm = None
                return pid, comm
    return None


def describe_holder(port: int, bind_ip: str) -> str | None:
    """Human fragment like ``process 1234 (docker-proxy)``; None when unknown."""
    found = holder_of(port, bind_ip)
    if found is None:
        return None
    pid, comm = found
    return f"process {pid} ({comm})" if comm else f"process {pid}"
