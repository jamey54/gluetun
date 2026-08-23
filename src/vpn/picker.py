"""Interactive server selection UI."""

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

from vpn.servers import SERVER_SEP, ServerRow, sorted_server_rows
from vpn.textutil import fold, fold_mapped

VISIBLE_ROWS = 10

_STYLE = Style.from_dict(
    {
        "dim": "#6c6c6c",
        "pointer": "bold cyan",
        "selected": "bold reverse",
        "match": "bold ansiyellow",
        "hits": "ansigreen",
        "none": "ansired",
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
        self.prompt = prompt
        self.query = ""
        self.tokens: list[str] = []
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

    # -- state -------------------------------------------------------------

    def _set_query(self, query: str) -> None:
        self.query = query
        self.tokens = query.split()
        if not self.tokens:
            self.matches = list(range(len(self.rows)))
        else:
            self.matches = [
                i
                for i, (folded, _) in enumerate(self.folds)
                if all(token in folded for token in self.tokens)
            ]
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
        line, (folded, origin) = self.lines[idx], self.folds[idx]
        if not self.tokens:
            return [(False, line)]
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
        return [
            ("class:dim", f"{hits}/{len(self.rows)} · "),
            ("class:dim", "/"),
            (status, self.query),
        ]

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
