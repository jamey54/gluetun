"""Interactive server selection UI.

Filtering is column-scoped: `Tab`/`Shift-Tab` cycle an active column
(Provider, Protocol, Country, City), `Left`/`Right` cycle through that
column's distinct values, and typing narrows within it. Without an active
column, typed tokens match anywhere in the row as before.
"""

from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout.containers import HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import D
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.output import Output
from prompt_toolkit.styles import Style

from epoxy.servers import SERVER_SEP, ServerRow, sorted_server_rows
from epoxy.textutil import fold, fold_mapped

VISIBLE_ROWS = 10

COLUMNS = ("provider", "protocol", "country", "city")

_STYLE = Style.from_dict(
    {
        "dim": "#6c6c6c",
        "pointer": "bold cyan",
        "selected": "bold reverse",
        "match": "bold ansiyellow",
        "hits": "ansigreen",
        "none": "ansired",
        "col": "bold ansiyellow",
    }
)

_PROMPT_STYLE = "class:dim"

PickerRow = tuple[str, str, str, str, str]


class _ServerPicker:
    def __init__(self, rows: list[PickerRow], prompt: str) -> None:
        self.rows = rows
        self.values = [f"[{p}/{proto}] {c}{SERVER_SEP}{ci}" for p, proto, c, ci, _ in rows]
        self.width: dict[str, int] = {
            "provider": max(len(p) for p, *_ in rows),
            "protocol": max(len(proto) for _, proto, *_ in rows),
            "country": max(len(c) for _, _, c, *_ in rows),
            "city": max(len(ci) for _, _, _, ci, _ in rows),
        }
        self.lines = [self._line(row) for row in rows]
        self.folds: list[tuple[str, list[int]]] = [fold_mapped(line) for line in self.lines]
        self.cells = [list(row[:4]) for row in rows]
        self.col_folds: list[list[tuple[str, list[int]]]] = [
            [fold_mapped(cell) for cell in cells] for cells in self.cells
        ]
        self.col_offsets: list[int] = []
        pos = 0
        for column in COLUMNS:
            self.col_offsets.append(pos)
            pos += self.width[column] + 1
        self.col_labels: list[dict[str, str]] = [
            self._distinct_values(idx) for idx in range(len(COLUMNS))
        ]
        self.prompt = prompt
        self.query = ""
        self.tokens: list[str] = []
        self.active_col: int | None = None
        self.col_value: str | None = None  # folded exact value for active_col
        self.matches = list(range(len(rows)))
        self.cursor = 0
        self.offset = 0

    def _line(self, row: PickerRow) -> str:
        w = self.width
        provider, protocol, country, city, hostname = row
        return (
            f"{provider:<{w['provider']}} {protocol:<{w['protocol']}} "
            f"{country:<{w['country']}} {city:<{w['city']}} {hostname or '-'}"
        )

    def _distinct_values(self, idx: int) -> dict[str, str]:
        """Folded value -> display value for a column (first occurrence wins)."""
        seen: dict[str, str] = {}
        for cells in self.cells:
            if cells[idx]:
                seen.setdefault(fold(cells[idx]), cells[idx])
        return dict(sorted(seen.items()))

    # -- state -------------------------------------------------------------

    def _row_folded(self, idx: int) -> str:
        """The text tokens are matched against for a row (whole line or column)."""
        if self.active_col is None:
            return self.folds[idx][0]
        return self.col_folds[idx][self.active_col][0]

    def _set_query(self, query: str) -> None:
        self.query = query
        self.tokens = [token for token in (fold(t) for t in query.split()) if token]
        self.matches = []
        for idx, row in enumerate(self.rows):
            if (
                self.active_col is not None
                and self.col_value is not None
                and fold(row[self.active_col]) != self.col_value
            ):
                continue
            folded = self._row_folded(idx)
            if all(token in folded for token in self.tokens):
                self.matches.append(idx)
        self.cursor = 0
        self.offset = 0

    def _move(self, delta: int) -> None:
        self._select(self.cursor + delta)

    def _select(self, pos: int) -> None:
        if not self.matches:
            return
        self.cursor = max(0, min(pos, len(self.matches) - 1))
        self._clamp_offset()

    def _clamp_offset(self) -> None:
        if self.cursor < self.offset:
            self.offset = self.cursor
        elif self.cursor >= self.offset + VISIBLE_ROWS:
            self.offset = self.cursor - VISIBLE_ROWS + 1
        self.offset = max(0, self.offset)

    def _advance_col(self, delta: int) -> None:
        """Cycle the active filter column; entering/leaving column mode."""
        if self.active_col is None:
            target = 0 if delta > 0 else len(COLUMNS) - 1
        else:
            target = self.active_col + delta
        if 0 <= target < len(COLUMNS):
            self.active_col = target
            self.col_value = None
        else:
            self.active_col = None
            self.col_value = None
        self._set_query(self.query)

    def _cycle_value(self, delta: int) -> None:
        """Cycle the active column's exact filter among its distinct values."""
        if self.active_col is None:
            return
        values = list(self.col_labels[self.active_col])
        choices: list[str | None] = [None, *values]
        try:
            idx = choices.index(self.col_value)
        except ValueError:
            idx = 0
        self.col_value = choices[(idx + delta) % len(choices)]
        self._set_query(self.query)

    def _escape(self) -> None:
        if self.active_col is not None:
            self.active_col = None
            self.col_value = None
            self._set_query(self.query)
        else:
            self._set_query("")

    # -- rendering ---------------------------------------------------------

    def _match_spans(self, folded: str, origin: list[int]) -> list[tuple[int, int]]:
        """Accent-insensitive match spans (raw-text indices) for all query tokens, merged."""
        spans: list[tuple[int, int]] = []
        for token in self.tokens:
            start = 0
            while True:
                hit = folded.find(token, start)
                if hit < 0:
                    break
                spans.append((origin[hit], origin[hit + len(token) - 1] + 1))
                start = hit + len(token)
        merged: list[list[int]] = []
        for start, end in sorted(spans):
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        return [(start, end) for start, end in merged]

    def _segments(self, idx: int) -> list[tuple[bool, str]]:
        """Split line idx into (is_match, text) fragments for the current query."""
        line = self.lines[idx]
        if not self.tokens:
            return [(False, line)]
        if self.active_col is None:
            folded, origin = self.folds[idx]
        else:
            col_folded, col_origin = self.col_folds[idx][self.active_col]
            start = self.col_offsets[self.active_col]
            folded = col_folded
            origin = [o + start for o in col_origin]
        segments: list[tuple[bool, str]] = []
        pos = 0
        for a, b in self._match_spans(folded, origin):
            if a > pos:
                segments.append((False, line[pos:a]))
            segments.append((True, line[a:b]))
            pos = b
        if pos < len(line):
            segments.append((False, line[pos:]))
        return segments

    def _body(self) -> StyleAndTextTuples:
        frags: StyleAndTextTuples = []
        for pos in range(self.offset, min(self.offset + VISIBLE_ROWS, len(self.matches))):
            idx = self.matches[pos]
            selected = pos == self.cursor
            prefix = ("class:pointer", "❯ ") if selected else ("", "  ")  # noqa: RUF001
            frags.append(prefix)
            for is_match, text in self._segments(idx):
                style = "class:selected" if selected else ""
                if is_match:
                    style = f"{style} class:match".strip()
                frags.append((style, text))
            frags.append(("", "\n"))
        return frags

    def _footer(self) -> StyleAndTextTuples:
        hits = len(self.matches)
        status = "class:hits" if hits else "class:none"
        frags: StyleAndTextTuples = [("class:dim", f"{hits}/{len(self.rows)} · ")]
        if self.active_col is not None:
            column = COLUMNS[self.active_col]
            if self.col_value is not None:
                label = self.col_labels[self.active_col][self.col_value]
                frags.append(("class:col", f"[{column}: {label}] "))
            else:
                frags.append(("class:col", f"[{column}] "))
            frags.append(("class:dim", "Tab cols · ←/→ value "))
        else:
            frags.append(("class:dim", "Tab: filter a column "))
        frags.append(("class:dim", "/"))
        frags.append((status, self.query))
        return frags

    # -- run ---------------------------------------------------------------

    def run(
        self,
        input: Input | None = None,
        output: Output | None = None,
    ) -> str | None:
        kb = KeyBindings()

        @kb.add("up")
        @kb.add("c-p")
        def _up(event: KeyPressEvent) -> None:
            self._move(-1)

        @kb.add("down")
        @kb.add("c-n")
        def _down(event: KeyPressEvent) -> None:
            self._move(1)

        @kb.add("pageup")
        def _pageup(event: KeyPressEvent) -> None:
            self._move(-VISIBLE_ROWS)

        @kb.add("pagedown")
        def _pagedown(event: KeyPressEvent) -> None:
            self._move(VISIBLE_ROWS)

        @kb.add("home")
        def _home(event: KeyPressEvent) -> None:
            self._select(0)

        @kb.add("end")
        def _end(event: KeyPressEvent) -> None:
            self._select(len(self.matches) - 1)

        @kb.add("backspace")
        def _backspace(event: KeyPressEvent) -> None:
            self._set_query(self.query[:-1])

        @kb.add("tab")
        def _col_next(event: KeyPressEvent) -> None:
            self._advance_col(1)

        @kb.add("s-tab")
        def _col_prev(event: KeyPressEvent) -> None:
            self._advance_col(-1)

        @kb.add("left")
        def _value_prev(event: KeyPressEvent) -> None:
            self._cycle_value(-1)

        @kb.add("right")
        def _value_next(event: KeyPressEvent) -> None:
            self._cycle_value(1)

        @kb.add("escape")
        def _escape_key(event: KeyPressEvent) -> None:
            self._escape()

        @kb.add("enter")
        def _enter(event: KeyPressEvent) -> None:
            if self.matches:
                event.app.exit(result=self.values[self.matches[self.cursor]])

        @kb.add("c-c")
        @kb.add("c-q")
        def _cancel(event: KeyPressEvent) -> None:
            event.app.exit(result=None)

        @kb.add(Keys.Any)
        def _type(event: KeyPressEvent) -> None:
            if event.data.isprintable():
                self._set_query(self.query + fold(event.data))

        app: Application[str] = Application(
            layout=Layout(
                HSplit(
                    [
                        Window(
                            FormattedTextControl(lambda: [(_PROMPT_STYLE, self.prompt)]),
                            height=D.exact(1),
                        ),
                        Window(
                            FormattedTextControl(self._body, show_cursor=False),
                            height=D.exact(VISIBLE_ROWS),
                        ),
                        Window(FormattedTextControl(self._footer), height=D.exact(1)),
                    ]
                )
            ),
            key_bindings=kb,
            style=_STYLE,
            full_screen=False,
            input=input,
            output=output,
        )
        result: str | None = app.run()
        return result


def select_server(
    by_provider: dict[str, list[ServerRow]],
    prompt: str = "Select server: ",
    input: Input | None = None,
    output: Output | None = None,
) -> str | None:
    """Interactive server selection with live filtering."""
    rows = sorted_server_rows(by_provider)
    if not rows:
        return None
    return _ServerPicker(rows, prompt).run(input=input, output=output)


class _InstancePicker(_ServerPicker):
    """Compact picker over instances, sharing the server picker's engine.

    Rows render as ``name (state)`` and selection returns the bare instance
    name. The column-scoped filtering machinery is inherited but unused here.
    """

    def __init__(self, instances: list[tuple[str, str]], prompt: str) -> None:
        rows: list[PickerRow] = []
        for name, state in instances:
            rows.append((name, "", "", "", state))
        super().__init__(rows, prompt)
        self.values = [name for name, _ in instances]

    def _line(self, row: PickerRow) -> str:
        name, _, _, _, state = row
        return f"{name} ({state})" if state else name

    def _footer(self) -> StyleAndTextTuples:
        hits = len(self.matches)
        status = "class:hits" if hits else "class:none"
        return [
            ("class:dim", f"{hits}/{len(self.rows)} · "),
            ("class:dim", "/"),
            (status, self.query),
        ]


def select_instance(
    instances: list[tuple[str, str]],
    prompt: str = "Select instance: ",
    input: Input | None = None,
    output: Output | None = None,
) -> str | None:
    """Interactive instance selection returning the chosen name (None on cancel)."""
    if not instances:
        return None
    return _InstancePicker(instances, prompt).run(input=input, output=output)
