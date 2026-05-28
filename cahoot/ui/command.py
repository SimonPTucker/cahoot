"""Single-line command input.

Posts a :class:`CommandSubmitted` message to the parent app on Enter.
The app handles parsing + execution; this widget only owns the input
itself.
"""

from __future__ import annotations

from textual.message import Message
from textual.widgets import Input

__all__ = ["CommandBox", "CommandSubmitted"]


class CommandSubmitted(Message):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class CommandBox(Input):
    """Always-focused bottom-of-screen input."""

    def __init__(self) -> None:
        super().__init__(placeholder="type a command (e.g. /help) or broadcast text…")

    def on_mount(self) -> None:
        self.border_title = "command"
        self.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value
        self.value = ""
        if text.strip():
            self.post_message(CommandSubmitted(text))
