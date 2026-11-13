"""Conversation rules, enforced in code so agents cannot loop forever or talk privately to each other.

Rules:
* Human in the channel -> a line starting with nicks (``nick: ...``, ``nick1, nick2: ...``, ``@nick``) is answered
  only by those agents; ``all:`` / ``everyone:`` / ``agents:`` or a line without an address is answered by every
  agent (``reply_to_unaddressed_human``, default on; when off, only addressed/mentioned agents answer).
  Every agent still reads every line, so all of them see the question and the answers.
* Agent in the channel -> another agent replies only when explicitly addressed at the *start* of the line
  (``nick: ...``) and the agent-turn budget is not used up. ``all:`` from an agent does not trigger anyone.
* Turn budget: at most ``max_agent_turns`` agent turns (a multi-line message is one turn) in the channel since the last human line. A human line
  resets it. Replies triggered by a human are always allowed.
* Private messages: only humans are answered; agents never PM each other.
* Replies: "PASS" -> stay silent; own-nick prefix stripped; cut where the model invents other speakers' lines; length capped; near-duplicates of own recent lines dropped.
"""

from __future__ import annotations

import difflib
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

from .config import PolicyConfig

BROADCAST_NAMES = ("all", "everyone", "agents")
TURN_GAP = 5.0  # seconds
_SPEAKER_RE = re.compile(r"^\s*<@?([^>\s]+)>")
_PASS_RE = re.compile(r"^\W*pass\W*$", re.IGNORECASE)


@dataclass
class Decision:
    reply: bool
    reason: str
    human_triggered: bool = False


def _norm(nick: str) -> str:
    return nick.lower()


def is_pass(text: str) -> bool:
    """True if a model reply means "stay silent" (PASS, possibly with formatting or an address prefix)."""
    return bool(_PASS_RE.match(re.sub(r"^\s*(?:<[^>]+>|[^\s:]+:)\s*", "", text or "")))


def _strip_address(text: str) -> str:
    """Lowercased text without a leading ``nick:`` / ``a, b:`` address (for duplicate detection)."""
    if leading_addressees(text):
        text = re.sub(r"^\s*((?:@?[^\s:,]+\s*,\s*)*@?[^\s:,]+)\s*[:,]\s+", "", text, count=1)
    return text.strip().lower()


def leading_addressees(text: str) -> list[str]:
    """Nicks addressed at the start of a line: ``a: hi``, ``a, b: hi``, ``@a hi``, ``a, hi`` -> lowercase names."""
    match = re.match(r"^\s*((?:@?[^\s:,]+\s*,\s*)*@?[^\s:,]+)\s*[:,]\s+", text) or re.match(r"^\s*@(\S+)\s", text)
    if not match:
        return []
    return [_norm(p.strip().lstrip("@")) for p in re.split(r"\s*,\s*", match.group(1)) if p.strip()]


def mentions(text: str, nick: str) -> bool:
    """True if ``nick`` appears as a whole word anywhere in the text (case-insensitive)."""
    return (
        re.search(rf"(?<![\w\[\]\\`^{{|}}-])@?{re.escape(nick)}(?![\w\[\]\\`^{{|}}-])", text, re.IGNORECASE) is not None
    )


class ConversationPolicy:
    def __init__(
        self,
        nick: str,
        humans: list[str],
        config: PolicyConfig,
        clock: Callable[[], float] = time.monotonic,
        agents: list[str] | None = None,
    ) -> None:
        self.nick = nick
        self.humans = {_norm(h) for h in humans}
        # Nicks whose "<nick> ..." lines a model must not invent. Grows with everyone seen in the channel.
        self.known_speakers = self.humans | {_norm(a) for a in agents or []} | {_norm(nick)}
        self.config = config
        self.clock = clock
        self.agent_streak = 0  # agent turns in the channel since the last human line
        self._last_speaker: str | None = None
        self._last_line_at = 0.0
        self._recent_own: deque[str] = deque(maxlen=max(1, config.dedupe_window))
        self._last_sent: float | None = None

    def is_human(self, nick: str) -> bool:
        return _norm(nick) in self.humans

    # -------------------------------------------------------------- incoming
    def observe_channel(self, sender: str) -> None:
        """Count a line someone else posted in the channel (call for every live channel line)."""
        self.known_speakers.add(_norm(sender))
        if self.is_human(sender):
            self.agent_streak = 0
            self._last_speaker = _norm(sender)
            self._last_line_at = self.clock()
        else:
            self._count_agent_turn(sender)

    def _count_agent_turn(self, sender: str) -> None:
        """Consecutive lines of one speaker within TURN_GAP seconds are one turn (a multi-line reply)."""
        now = self.clock()
        if not (_norm(sender) == self._last_speaker and now - self._last_line_at < TURN_GAP):
            self.agent_streak += 1
        self._last_speaker = _norm(sender)
        self._last_line_at = now

    def human_addressing(self, text: str) -> str:
        """How a human line relates to this agent: "me", "broadcast", "other" (addressed to someone else),
        "mention" (no address, but our nick appears) or "none"."""
        names = leading_addressees(text)
        if _norm(self.nick) in names:
            return "me"
        if any(n in BROADCAST_NAMES for n in names):
            return "broadcast"
        # A line starting with a known nick is for that nick only, even if it mentions us ("a1: ask a2 about X").
        if any(n in self.known_speakers for n in names):
            return "other"
        return "mention" if mentions(text, self.nick) else "none"

    def is_addressed_by_agent(self, text: str) -> bool:
        """Agents trigger each other only by an explicit leading ``nick:`` (never by all: or mentions)."""
        return _norm(self.nick) in leading_addressees(text)

    def budget_left(self) -> int:
        return max(0, self.config.max_agent_turns - self.agent_streak)

    def decide_channel(self, sender: str, text: str) -> Decision:
        if _norm(sender) == _norm(self.nick):
            return Decision(False, "own message")
        if self.is_human(sender):
            how = self.human_addressing(text)
            if how in ("me", "broadcast", "mention"):
                return Decision(True, f"human line ({how})", human_triggered=True)
            if how == "none" and self.config.reply_to_unaddressed_human:
                return Decision(True, "unaddressed human line: everyone answers", human_triggered=True)
            return Decision(False, "human line addressed to someone else" if how == "other" else "not addressed")
        if not self.is_addressed_by_agent(text):
            return Decision(False, "agent line not addressed to me")
        if self.budget_left() <= 0:
            return Decision(False, f"agent-turn budget used up ({self.config.max_agent_turns}); waiting for a human")
        return Decision(True, "addressed by agent")

    def decide_private(self, sender: str) -> Decision:
        if self.is_human(sender):
            return Decision(True, "private message from human", human_triggered=True)
        return Decision(False, "private messages between agents are not allowed")

    # -------------------------------------------------------------- outgoing
    def may_send_channel(self, human_triggered: bool) -> bool:
        """Re-check right before sending: other agents may have used the budget while we were generating."""
        return human_triggered or self.budget_left() > 0

    def cooldown_remaining(self) -> float:
        if self._last_sent is None:
            return 0.0
        return max(0.0, self.config.min_reply_interval - (self.clock() - self._last_sent))

    def clean_reply(self, text: str, recent_lines: list[str] | None = None) -> str | None:
        """Normalise a model reply. None means: do not send anything.

        ``recent_lines``: other people's recent lines in this conversation; a reply that parrots one is dropped."""
        text = (text or "").strip()
        # Models sometimes prefix their own name / IRC formatting: "<a1_qwen> ...", "a1_qwen: ..."
        own = re.escape(self.nick)
        text = re.sub(rf"^\s*(?:<{own}>|\[{own}\]|\*\*{own}\*\*:?|{own}:)\s*", "", text, flags=re.IGNORECASE).strip()
        if not text or _PASS_RE.match(text):
            return None
        lines = []
        for ln in text.splitlines():
            speaker = _SPEAKER_RE.match(ln)
            if speaker and _norm(speaker.group(1)) in self.known_speakers:
                break  # model started writing other people's lines (IRC log continuation): cut here
            if ln.strip():
                lines.append(ln.rstrip())
        if not lines:
            return None
        if len(lines) > self.config.max_reply_lines:
            lines = lines[: self.config.max_reply_lines]
            lines[-1] += " [...]"
        text = "\n".join(lines)
        if len(text) > self.config.max_reply_chars:
            text = text[: self.config.max_reply_chars].rsplit(" ", 1)[0] + " [...]"
        if self.is_duplicate(text, recent_lines or []):
            return None
        return text

    def is_duplicate(self, text: str, recent_lines: list[str] | None = None) -> bool:
        body = _strip_address(text)
        others = [_strip_address(t) for t in (recent_lines or [])[-self.config.dedupe_window :]]
        return any(
            difflib.SequenceMatcher(None, body, prev).ratio() >= self.config.dedupe_similarity
            for prev in [*self._recent_own, *others]
            if prev
        )

    def note_sent(self, text: str, in_channel: bool) -> None:
        self._recent_own.append(_strip_address(text))
        self._last_sent = self.clock()
        if in_channel:
            self._count_agent_turn(self.nick)
