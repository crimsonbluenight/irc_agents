"""Per-conversation rolling history, converted to chat messages for the backend."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .backends.base import ChatMessage


@dataclass(frozen=True)
class Line:
    sender: str
    text: str
    own: bool = False
    action: bool = False

    def render(self) -> str:
        return f"* {self.sender} {self.text}" if self.action else f"<{self.sender}> {self.text}"


class Memory:
    """Conversation key is a channel name or ``pm:<nick>``."""

    def __init__(self, max_lines: int) -> None:
        self.max_lines = max_lines
        self._conversations: dict[str, deque[Line]] = {}

    def add(self, key: str, line: Line) -> None:
        self._conversations.setdefault(key, deque(maxlen=self.max_lines)).append(line)

    def lines(self, key: str) -> list[Line]:
        return list(self._conversations.get(key, ()))

    def to_messages(self, key: str, system_prompt: str, reminder: str = "") -> list[ChatMessage]:
        """Own lines become ``assistant`` turns; everyone else's consecutive lines are merged into one ``user`` turn
        rendered as an IRC log (``<nick> text``) so the model knows who said what. ``reminder`` is appended to the
        last user turn (keeps small models from continuing the log as other speakers)."""
        messages = [ChatMessage("system", system_prompt)]
        pending: list[str] = []
        for line in self.lines(key):
            if line.own:
                if pending:
                    messages.append(ChatMessage("user", "\n".join(pending)))
                    pending = []
                messages.append(ChatMessage("assistant", line.text))
            else:
                pending.append(line.render())
        if pending:
            messages.append(ChatMessage("user", "\n".join(pending)))
        if reminder and messages[-1].role == "user":
            messages[-1] = ChatMessage("user", messages[-1].content + "\n\n" + reminder)
        return messages
