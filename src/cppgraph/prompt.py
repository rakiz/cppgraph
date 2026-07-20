"""Interactive prompting, with a plain-stdlib default and an optional rich/
questionary TUI.

Every interactive command talks to a `Prompter` rather than to `input()`/`print()`
directly, so the whole flow is scriptable in tests (inject a `Prompter` backed by a
canned answer list) and the terminal experience can be upgraded to selectable menus
without touching the command logic.

`make_prompter()` returns the TUI-backed prompter when `rich`/`questionary` are
installed and stdin is a real terminal, else the stdlib one — both honour the same
small surface: `note`, `panel`, `text`, `confirm`, `select`.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence


class Prompter:
    """Plain-stdlib prompter: prints with `print_fn`, reads lines with `input_fn`.
    Selectable menus are rendered as a numbered list read from `input_fn`, so a
    scripted `input_fn` drives the whole flow in tests."""

    def __init__(
        self, input_fn: Callable[[str], str] = input, print_fn: Callable[..., None] = print
    ):
        self._input = input_fn
        self._print = print_fn

    def note(self, *lines: str) -> None:
        for line in lines:
            self._print(line)

    def panel(self, title: str, rows: Sequence[tuple[str, str]]) -> None:
        """A small labelled info block (artifact details, etc.)."""
        self._print(f"{title}:")
        for label, value in rows:
            self._print(f"  {label}: {value}")

    def _read(self, prompt: str) -> str:
        try:
            return self._input(prompt).strip()
        except EOFError:
            return ""

    def text(self, message: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        return self._read(f"{message}{suffix}: ") or default

    def confirm(self, message: str, default: bool = False) -> bool:
        hint = "Y/n" if default else "y/N"
        raw = self._read(f"{message} ({hint}) ").lower()
        if not raw:
            return default
        return raw in ("y", "yes", "o", "oui")

    def select(self, message: str, options: Sequence[tuple[str, str]], default: str) -> str:
        """Pick one value from `options` (each `(value, label)`). Returns the chosen
        value; empty input accepts `default`."""
        self._print(message)
        default_idx = 1
        for i, (value, label) in enumerate(options, 1):
            marker = ""
            if value == default:
                marker = " (default)"
                default_idx = i
            self._print(f"  [{i}] {label}{marker}")
        raw = self._read(f"Choose [{default_idx}]: ")
        if not raw:
            return default
        try:
            idx = int(raw)
        except ValueError:
            return default
        if 1 <= idx <= len(options):
            return options[idx - 1][0]
        return default


class _TuiPrompter(Prompter):
    """rich panels + questionary menus. Falls back to the stdlib behaviour for any
    piece that isn't available at call time."""

    def __init__(self) -> None:
        super().__init__()
        import questionary
        import rich.console

        self._q = questionary
        self._console = rich.console.Console()

    def panel(self, title: str, rows: Sequence[tuple[str, str]]) -> None:
        from rich.panel import Panel
        from rich.table import Table

        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold")
        table.add_column()
        for label, value in rows:
            table.add_row(label, value)
        self._console.print(Panel(table, title=title, expand=False))

    def note(self, *lines: str) -> None:
        for line in lines:
            self._console.print(line)

    def text(self, message: str, default: str = "") -> str:
        return self._q.text(message, default=default).ask() or default

    def confirm(self, message: str, default: bool = False) -> bool:
        answer = self._q.confirm(message, default=default).ask()
        return default if answer is None else bool(answer)

    def select(self, message: str, options: Sequence[tuple[str, str]], default: str) -> str:
        choices = []
        default_choice = None
        for value, label in options:
            c = self._q.Choice(title=label, value=value)
            choices.append(c)
            if value == default:
                default_choice = c
        answer = self._q.select(message, choices=choices, default=default_choice).ask()
        return default if answer is None else answer


def tui_available() -> bool:
    try:
        import questionary  # noqa: F401
        import rich  # noqa: F401
    except ImportError:
        return False
    return True


def make_prompter(*, force_plain: bool = False) -> Prompter:
    """The best prompter for the current context: the rich/questionary TUI when it
    is installed and stdin/stdout are a terminal, otherwise the stdlib one."""
    if force_plain or not (sys.stdin.isatty() and sys.stdout.isatty()) or not tui_available():
        return Prompter()
    try:
        return _TuiPrompter()
    except Exception:
        return Prompter()
