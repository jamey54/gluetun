"""Tests for host listener lookup (naming port holders)."""

from epoxy import listeners
from epoxy.listeners import describe_holder, holder_of

_HEADER = "sl local_address rem_address st tx rx tr tm->when retrnsmt uid timeout inode\n"


def _row(local: str, inode: int, state: str = "0A") -> str:
    return (
        f"0: {local} 00000000:0000 {state} 00000000:00000000 00:00000000 "
        f"00000000 0 0 {inode} 1 0000000000000000 100 0 0 10 0\n"
    )


def _proc_tree(tmp_path, tcp="", tcp6="", holders=()):
    net = tmp_path / "net"
    net.mkdir(exist_ok=True)
    (net / "tcp").write_text(_HEADER + tcp)
    (net / "tcp6").write_text(_HEADER + tcp6)
    for pid, comm, inodes in holders:
        fd_dir = tmp_path / str(pid) / "fd"
        fd_dir.mkdir(parents=True)
        if comm is not None:
            (tmp_path / str(pid) / "comm").write_text(comm)
        for i, inode in enumerate(inodes):
            (fd_dir / str(i)).symlink_to(f"socket:[{inode}]")
    return tmp_path


def test_holder_exact_ipv4_match(tmp_path):
    root = _proc_tree(
        tmp_path, tcp=_row("0100007F:0BB8", 12345), holders=[(4242, "docker-proxy\n", [12345])]
    )
    assert holder_of(3000, "127.0.0.1", root) == (4242, "docker-proxy")


def test_describe_holder_fragment(monkeypatch):
    monkeypatch.setattr(listeners, "holder_of", lambda *args, **kwargs: (4242, "docker-proxy"))
    assert describe_holder(3000, "127.0.0.1") == "process 4242 (docker-proxy)"
    monkeypatch.setattr(listeners, "holder_of", lambda *args, **kwargs: (7, None))
    assert describe_holder(3000, "127.0.0.1") == "process 7"
    monkeypatch.setattr(listeners, "holder_of", lambda *args, **kwargs: None)
    assert describe_holder(3000, "127.0.0.1") is None


def test_holder_wildcard_matches_any_bind(tmp_path):
    root = _proc_tree(tmp_path, tcp=_row("00000000:0BB8", 111), holders=[(7, "svc", [111])])
    assert holder_of(3000, "127.0.0.1", root) == (7, "svc")
    assert holder_of(3000, "0.0.0.0", root) == (7, "svc")


def test_holder_ipv6_forms(tmp_path):
    root = _proc_tree(
        tmp_path,
        tcp6=_row("00000000000000000000000000000000:0BB8", 21)
        + _row("00000000000000000000000000000001:0BB9", 22),
        holders=[(9, "v6svc", [21, 22])],
    )
    assert holder_of(3000, "::", root) == (9, "v6svc")  # wildcard
    assert holder_of(3001, "::1", root) == (9, "v6svc")  # exact ::1


def test_holder_ipv4_mapped_ipv6(tmp_path):
    # ::ffff:127.0.0.1 listener answers a 127.0.0.1 bind.
    root = _proc_tree(
        tmp_path,
        tcp6=_row("00000000000000000000FFFF7F000001:0BB8", 31),
        holders=[(11, "mapped", [31])],
    )
    assert holder_of(3000, "127.0.0.1", root) == (11, "mapped")


def test_holder_ignores_non_listen_and_wrong_port(tmp_path):
    root = _proc_tree(
        tmp_path,
        tcp=_row("0100007F:0BB8", 41, state="01") + _row("0100007F:0BC0", 42),
        holders=[(13, "x", [41, 42])],
    )
    assert holder_of(3000, "127.0.0.1", root) is None
    assert holder_of(3008, "127.0.0.1", root) == (13, "x")


def test_holder_falls_back_to_port_only_match(tmp_path):
    """A same-port listener on an unrelated address still names *some* holder."""
    root = _proc_tree(tmp_path, tcp=_row("C0A80105:0BB8", 51), holders=[(15, "other-if", [51])])
    assert holder_of(3000, "127.0.0.1", root) == (15, "other-if")


def test_holder_missing_comm_names_pid_only(tmp_path):
    root = _proc_tree(tmp_path, tcp=_row("0100007F:0BB8", 61), holders=[(17, None, [61])])
    assert holder_of(3000, "127.0.0.1", root) == (17, None)


def test_holder_first_pid_wins_and_missing_proc_is_none(tmp_path):
    root = _proc_tree(
        tmp_path,
        tcp=_row("0100007F:0BB8", 71),
        holders=[(20, "second", [71]), (3, "first", [71])],
    )
    assert holder_of(3000, "127.0.0.1", root) == (3, "first")
    assert holder_of(3000, "127.0.0.1", tmp_path / "no-such-root") is None
    assert holder_of(9999, "127.0.0.1", root) is None
