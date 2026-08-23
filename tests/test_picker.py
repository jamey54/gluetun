"""Tests for the interactive server picker (state machine, no terminal needed)."""

from itertools import pairwise

from vpn.picker import _ServerPicker, select_server
from vpn.textutil import fold_mapped as _fold

ROWS = [
    ("surfshark", "wireguard", "Netherlands", "Amsterdam", "nl-ams-1"),
    ("surfshark", "openvpn", "Germany", "Munich", "de-muc-1"),
    ("protonvpn", "wireguard", "United States", "Boston", "us-bos-1"),
]


def make_picker():
    return _ServerPicker(ROWS, prompt="Select: ")


# ---------------------------------------------------------------------------
# folding
# ---------------------------------------------------------------------------


def test_fold_maps_origins():
    folded, origin = _fold("São")
    assert folded == "sao"
    assert origin == [0, 1, 2]  # 'ã' folds to one char


def test_fold_drops_unmappable_chars():
    # 'æ' has no NFKD/ASCII fold and is dropped, matching strip_accents behavior
    folded, origin = _fold("æA")
    assert folded == "a"
    assert origin == [1]


# ---------------------------------------------------------------------------
# filtering
# ---------------------------------------------------------------------------


def test_initial_state_matches_all():
    p = make_picker()
    assert len(p.matches) == 3
    assert p.cursor == 0


def test_query_single_token():
    p = make_picker()
    p._set_query("germany")
    assert p.matches == [1]


def test_query_multi_token_is_and():
    p = make_picker()
    p._set_query("surfshark wireguard")
    assert p.matches == [0]
    p._set_query("surfshark protonvpn")
    assert p.matches == []


def test_query_matches_provider_and_protocol_columns():
    p = make_picker()
    p._set_query("proton")
    assert p.matches == [2]
    p._set_query("openvpn")
    assert p.matches == [1]


def test_query_accent_insensitive():
    p = _ServerPicker([("p", "wireguard", "São Paulo", "SP", "h")], prompt="")
    p._set_query("sao paulo")
    assert p.matches == [0]


def test_clearing_query_restores_all():
    p = make_picker()
    p._set_query("zzz")
    assert p.matches == []
    p._set_query("")
    assert len(p.matches) == 3


def test_query_reset_moves_cursor_to_top():
    p = make_picker()
    p._move(2)
    p._set_query("u")
    assert p.cursor == 0


# ---------------------------------------------------------------------------
# navigation / paging
# ---------------------------------------------------------------------------


def test_move_clamps_bounds():
    p = make_picker()
    p._move(-5)
    assert p.cursor == 0
    p._move(100)
    assert p.cursor == 2


def test_no_matches_navigation_is_noop():
    p = make_picker()
    p._set_query("zzz")
    p._move(1)
    assert p.cursor == 0


def test_offset_follows_cursor_down():
    picker_rows = [(str(i), "wg", f"c{i}", "", "") for i in range(25)]
    p = _ServerPicker(picker_rows, prompt="")
    p._move(9)  # last visible row, window unchanged
    assert (p.offset, p.cursor) == (0, 9)
    p._move(1)  # cursor leaves visible window -> scroll by one
    assert (p.offset, p.cursor) == (1, 10)


def test_pageup_pagedown():
    picker_rows = [(str(i), "wg", f"c{i}", "", "") for i in range(25)]
    p = _ServerPicker(picker_rows, prompt="")
    p._select(15)
    p._move(-10)
    assert p.cursor == 5
    p._move(20)
    assert p.cursor == min(25, len(picker_rows)) - 1


# ---------------------------------------------------------------------------
# highlight spans
# ---------------------------------------------------------------------------


def test_match_spans_merge_overlaps():
    p = make_picker()
    p._set_query("am st")  # overlapping spans must merge, not crash rendering
    folded, origin = p.folds[p.matches[0]]
    spans = p._match_spans(folded, origin)
    for (_, b), (a2, _) in pairwise(spans):
        assert a2 > b  # strictly increasing after merge


def test_segments_cover_whole_line():
    p = make_picker()
    p._set_query("bos")
    idx = p.matches[0]
    segments = p._segments(idx)
    assert "".join(text for _, text in segments) == p.lines[idx]
    assert any(is_match for is_match, _ in segments)


def test_segments_without_query_single_fragment():
    p = make_picker()
    assert p._segments(0) == [(False, p.lines[0])]


# ---------------------------------------------------------------------------
# values returned on selection
# ---------------------------------------------------------------------------


def test_values_carry_provider_and_protocol():
    p = make_picker()
    assert p.values[0] == "[surfshark/wireguard] Netherlands - Amsterdam"


# ---------------------------------------------------------------------------
# select_server entry point
# ---------------------------------------------------------------------------


def test_select_server_empty_returns_none():
    assert select_server({}) is None
