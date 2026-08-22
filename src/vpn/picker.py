"""Interactive server selection."""

from questionary import Separator, Choice
import questionary

from vpn.servers import SERVER_SEP, _sorted_server_rows


def select_server(by_provider, prompt="Select server: "):
    """Interactive server selection with provider grouping."""
    from questionary.prompts.common import InquirerControl

    _orig_filtered = InquirerControl.filtered_choices.fget

    @property
    def _filtered_with_separators(self):
        if not self.search_filter:
            return self.choices
        filtered = [
            c for c in self.choices
            if isinstance(c, Separator)
            or self.search_filter.lower() in c.title.lower()
        ]
        self.found_in_search = any(
            not isinstance(c, Separator) for c in filtered
        )
        return filtered if self.found_in_search else self.choices

    InquirerControl.filtered_choices = _filtered_with_separators

    choices = []
    rows = _sorted_server_rows(by_provider)
    widths = {
        key: max(len(r[i]) for r in rows)
        for i, key in enumerate(("provider", "country", "city"))
    }
    for provider, country, city, hostname in rows:
        title = (
            f"{provider:<{widths['provider']}}  "
            f"{country:<{widths['country']}}  "
            f"{city:<{widths['city']}}  "
            f"{hostname or '-'}"
        )
        value = f"[{provider}] {country}{SERVER_SEP}{city}"
        choices.append(Choice(title=title, value=value))

    try:
        return questionary.select(
            message=prompt,
            choices=choices,
            use_search_filter=True,
            use_jk_keys=False,
        ).ask()
    finally:
        InquirerControl.filtered_choices = _orig_filtered
