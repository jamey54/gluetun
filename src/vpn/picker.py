"""Interactive server selection UI."""

from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import D
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.keys import Keys
from prompt_toolkit.styles import Style

from vpn.servers import SERVER_SEP, _sorted_server_rows, strip_accents

VISIBLE_ROWS = 10

_STYLE = Style.from_dict({
    "dim": "#6c6c6c",
    "pointer": "bold cyan",
    "selected": "bold reverse",
    "match": "bold ansiyellow",
    "hits": "ansigreen",
    "none": "ansired",
})


def _fold(text):
    """Return (lowercase accent-free text, orig index per folded char)."""
    folded, origin = [], []
    for i, ch in enumerate(text):
        for fch in strip_accents(ch).lower():
            folded.append(fch)
            origin.append(i)
    return "".join(folded), origin


class _ServerPicker:
    def __init__(self, rows, prompt):
        self.rows = rows
        self.values = [f"[{p}] {c}{SERVER_SEP}{ci}" for p, c, ci, _ in rows]
        self.width = max(len(p) for p, *_ in rows)
        self.lines = [self._line(row) for row in rows]
        self.folds = [_fold(line) for line in self.lines]
        self.prompt = prompt
        self.query = ""
        self.matches = list(range(len(rows)))
        self.cursor = 0
        self.offset = 0

    def _line(self, row):
        provider, country, city, hostname = row
        return (
            f"{provider:<{self.width}} {country} {city} {hostname or '-'}"
        )

    # -- state -------------------------------------------------------------

    def _set_query(self, query):
        self.query = query
        if not query:
            self.matches = list(range(len(self.rows)))
        else:
            self.matches = [
                i for i, (folded, _) in enumerate(self.folds) if query in folded
            ]
        self.cursor = 0
        self.offset = 0

    def _move(self, delta):
        self._select(self.cursor + delta)

    def _select(self, pos):
        if not self.matches:
            return
        self.cursor = max(0, min(pos, len(self.matches) - 1))
        self._clamp_offset()

    def _clamp_offset(self):
        if self.cursor < self.offset:
            self.offset = self.cursor
        elif self.cursor >= self.offset + VISIBLE_ROWS:
            self.offset = self.cursor - VISIBLE_ROWS + 1
        self.offset = max(0, self.offset)

    # -- rendering ---------------------------------------------------------

    def _segments(self, idx):
        """Split line idx into (is_match, text) fragments for the current query."""
        line, (folded, origin) = self.lines[idx], self.folds[idx]
        if not self.query:
            return [(False, line)]
        spans, start = [], 0
        while True:
            hit = folded.find(self.query, start)
            if hit < 0:
                break
            spans.append((origin[hit], origin[hit + len(self.query) - 1] + 1))
            start = hit + len(self.query)
        segments, pos = [], 0
        for a, b in spans:
            if a < pos:
                continue
            if a > pos:
                segments.append((False, line[pos:a]))
            segments.append((True, line[a:b]))
            pos = b
        if pos < len(line):
            segments.append((False, line[pos:]))
        return segments

    def _body(self):
        frags = []
        for pos in range(self.offset, min(self.offset + VISIBLE_ROWS, len(self.matches))):
            idx = self.matches[pos]
            selected = pos == self.cursor
            prefix = ("class:pointer", "❯ ") if selected else ("", "  ")
            frags.append(prefix)
            for is_match, text in self._segments(idx):
                style = "class:selected" if selected else ""
                if is_match:
                    style = f"{style} class:match".strip()
                frags.append((style, text))
            frags.append(("", "\n"))
        return frags

    def _footer(self):
        hits = len(self.matches)
        status = "class:hits" if hits else "class:none"
        return [
            ("class:dim", f"{hits}/{len(self.rows)} · "),
            ("class:dim", "/"),
            (status, self.query),
        ]

    # -- run ---------------------------------------------------------------

    def run(self, input=None, output=None):
        kb = KeyBindings()

        @kb.add("up")
        @kb.add("c-p")
        def _(event):
            self._move(-1)

        @kb.add("down")
        @kb.add("c-n")
        def _(event):
            self._move(1)

        @kb.add("pageup")
        def _(event):
            self._move(-VISIBLE_ROWS)

        @kb.add("pagedown")
        def _(event):
            self._move(VISIBLE_ROWS)

        @kb.add("home")
        def _(event):
            self._select(0)

        @kb.add("end")
        def _(event):
            self._select(len(self.matches) - 1)

        @kb.add("backspace")
        def _(event):
            self._set_query(self.query[:-1])

        @kb.add("enter")
        def _(event):
            if self.matches:
                event.app.exit(result=self.values[self.matches[self.cursor]])

        @kb.add("c-c")
        @kb.add("c-q")
        def _(event):
            event.app.exit(result=None)

        @kb.add(Keys.Any)
        def _(event):
            if event.data.isprintable():
                self._set_query(self.query + _fold(event.data)[0])

        app = Application(
            layout=Layout(HSplit([
                Window(FormattedTextControl(lambda: [("class:dim", self.prompt)]),
                       height=D.exact(1)),
                Window(FormattedTextControl(self._body, show_cursor=False),
                       height=D.exact(VISIBLE_ROWS)),
                Window(FormattedTextControl(self._footer), height=D.exact(1)),
            ])),
            key_bindings=kb,
            style=_STYLE,
            full_screen=False,
            input=input,
            output=output,
        )
        return app.run()


def select_server(by_provider, prompt="Select server: ", input=None, output=None):
    """Interactive server selection with live filtering."""
    rows = _sorted_server_rows(by_provider)
    if not rows:
        return None
    return _ServerPicker(rows, prompt).run(input=input, output=output)
