"""Tests for the interactive server picker (state machine, no terminal needed)."""

from itertools import pairwise

from epoxy.picker import _InstancePicker, _ServerPicker, select_instance, select_server
from epoxy.textutil import fold_mapped as _fold

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


def test_query_ignores_non_foldable_tokens():
    # '⌘' folds to '', which would match every row; it must be dropped
    p = make_picker()
    p._set_query("⌘")
    assert len(p.matches) == 3
    p._set_query("⌘ germany")
    assert p.matches == [1]


# ---------------------------------------------------------------------------
# column-scoped filtering
# ---------------------------------------------------------------------------


def test_tab_cycles_active_column():
    p = make_picker()
    assert p.active_col is None
    p._advance_col(1)
    assert p.active_col == 0  # provider
    p._advance_col(1)
    assert p.active_col == 1  # protocol
    p._advance_col(3)  # wraps city -> back to all columns
    assert p.active_col is None
    p._advance_col(-1)  # wraps back from all
    assert p.active_col == 3  # city


def test_active_column_scopes_typed_query():
    p = make_picker()
    p._advance_col(1)  # provider column
    p._set_query("surf")
    assert p.matches == [0, 1]
    p._set_query("germany")  # not a provider -> no matches
    assert p.matches == []


def test_column_value_cycles_with_keys():
    p = make_picker()
    p._advance_col(1)  # provider
    p._cycle_value(1)  # -> protonvpn (sorted first)
    assert p.col_value == "protonvpn"
    assert p.matches == [2]
    p._cycle_value(1)  # -> surfshark
    assert p.col_value == "surfshark"
    assert p.matches == [0, 1]
    p._cycle_value(1)  # wraps to none -> all rows
    assert p.col_value is None
    assert len(p.matches) == 3


def test_column_value_cycles_backwards():
    p = make_picker()
    p._advance_col(1)
    p._cycle_value(-1)
    assert p.col_value == "surfshark"
    assert p.matches == [0, 1]


def test_column_value_and_typed_tokens_are_both_applied():
    p = make_picker()
    p._advance_col(1)
    p._advance_col(1)  # protocol column
    p._set_query("wireguard")
    assert p.matches == [0, 2]  # surfshark-wg + protonepoxy-wg
    p._cycle_value(1)  # openvpn contradicts the typed query
    assert p.col_value == "openvpn"
    assert p.matches == []
    p._cycle_value(1)  # wireguard agrees with it
    assert p.matches == [0, 2]
    p._cycle_value(1)  # back to no value filter
    assert p.col_value is None
    assert p.matches == [0, 2]


def test_column_value_highlight_spans_shifted_to_line():
    p = make_picker()
    p._advance_col(1)  # provider column
    p._set_query("proton")
    idx = p.matches[0]
    segments = p._segments(idx)
    assert "".join(text for _, text in segments) == p.lines[idx]
    hit = next(text for is_match, text in segments if is_match)
    assert hit == "proton"  # only the matched substring is highlighted


def test_escape_exits_column_mode_keeps_query():
    p = make_picker()
    p._advance_col(1)  # provider
    p._set_query("germany")
    assert p.matches == []
    p._escape()
    assert p.active_col is None
    assert p.matches == [1]  # query now applies globally


def test_escape_clears_query_when_no_column():
    p = make_picker()
    p._set_query("boston")
    assert p.matches == [2]
    p._escape()
    assert p.query == ""
    assert len(p.matches) == 3


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


# ---------------------------------------------------------------------------
# instance picker
# ---------------------------------------------------------------------------

INSTANCES = [("epoxy", "running"), ("plan-a", "stopped"), ("plan-b", "running")]


def make_instance_picker():
    return _InstancePicker(INSTANCES, prompt="Select instance: ")


def test_instance_picker_lines_show_name_and_state():
    p = make_instance_picker()
    assert p.lines == ["epoxy (running)", "plan-a (stopped)", "plan-b (running)"]


def test_instance_picker_values_are_bare_names():
    p = make_instance_picker()
    assert p.values == ["epoxy", "plan-a", "plan-b"]


def test_instance_picker_filters_by_name():
    p = make_instance_picker()
    p._set_query("plan")
    assert p.matches == [1, 2]
    p._set_query("plan-a")
    assert p.matches == [1]


def test_instance_picker_no_state_renders_name_only():
    p = _InstancePicker([("plan-a", "")], prompt="")
    assert p.lines == ["plan-a"]


def test_select_instance_empty_returns_none():
    assert select_instance([]) is None
