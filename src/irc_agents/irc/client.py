"""Minimal asyncio IRC client: registration, CAP negotiation, PING, reconnect, history detection."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from .protocol import IrcMessage, format_line, parse_line, split_text

log = logging.getLogger(__name__)

# server-time: timestamps on messages; batch: Ergo wraps join-replayed history in a "chathistory" batch.
WANTED_CAPS = {"server-time", "batch", "message-tags"}
# Messages whose server-time is older than our join by more than this are treated as replayed history.
HISTORY_SLACK = timedelta(seconds=2)


@dataclass
class ChatEvent:
    """A PRIVMSG/NOTICE/ACTION received from the server."""

    sender: str
    target: str  # channel name or our own nick
    text: str
    is_private: bool
    is_history: bool = False
    is_notice: bool = False
    is_action: bool = False
    time: datetime | None = None


EventHandler = Callable[[ChatEvent], Awaitable[None]]


def _parse_server_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class IrcConnection:
    """One IRC identity. Calls ``on_event`` for every chat message; reconnects with backoff until ``stop()``."""

    def __init__(
        self,
        host: str,
        port: int,
        nick: str,
        channels: list[str],
        on_event: EventHandler,
        *,
        realname: str = "irc-agents bot",
        password: str | None = None,
        line_delay: float = 0.25,
        max_backoff: float = 60.0,
    ) -> None:
        self.host = host
        self.port = port
        self.wanted_nick = nick
        self.nick = nick
        self.channels = channels
        self.on_event = on_event
        self.realname = realname
        self.password = password
        self.line_delay = line_delay
        self.max_backoff = max_backoff

        self.ready = asyncio.Event()  # registered and joined all channels
        self._writer: asyncio.StreamWriter | None = None
        self._send_lock = asyncio.Lock()
        self._stopping = False
        self._caps: set[str] = set()
        self._batches: dict[str, str] = {}  # ref -> batch type
        self._joined: set[str] = set()
        self._join_time: dict[str, datetime] = {}

    # ------------------------------------------------------------------ lifecycle
    async def run(self) -> None:
        backoff = 1.0
        while not self._stopping:
            try:
                await self._session()
                backoff = 1.0
            except (OSError, asyncio.IncompleteReadError, ConnectionError) as exc:
                if not self._stopping:
                    log.warning("[%s] connection error: %s", self.wanted_nick, exc)
            finally:
                self.ready.clear()
                self._writer = None
            if self._stopping:
                break
            log.info("[%s] reconnecting in %.0fs", self.wanted_nick, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self.max_backoff)

    async def stop(self, message: str = "bye") -> None:
        self._stopping = True
        if self._writer is not None:
            try:
                await self.send_raw(format_line("QUIT", message))
                self._writer.close()
            except (OSError, ConnectionError):
                pass

    async def _session(self) -> None:
        reader, writer = await asyncio.open_connection(self.host, self.port)
        self._writer = writer
        self._caps.clear()
        self._batches.clear()
        self._joined.clear()
        self.nick = self.wanted_nick
        log.info("[%s] connected to %s:%s", self.nick, self.host, self.port)
        await self.send_raw("CAP LS 302")
        if self.password:
            await self.send_raw(format_line("PASS", self.password))
        await self.send_raw(format_line("NICK", self.nick))
        await self.send_raw(format_line("USER", self.nick, "0", "*", self.realname))
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    raise ConnectionError("server closed the connection")
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line:
                    await self._handle(parse_line(line))
        finally:
            writer.close()

    # ------------------------------------------------------------------ sending
    async def send_raw(self, line: str) -> None:
        if self._writer is None:
            raise ConnectionError("not connected")
        async with self._send_lock:
            self._writer.write((line + "\r\n").encode())
            await self._writer.drain()

    async def privmsg(self, target: str, text: str) -> int:
        """Send text (split into several lines if needed). Returns number of lines sent."""
        lines = split_text(text)
        for i, line in enumerate(lines):
            if i:
                await asyncio.sleep(self.line_delay)
            await self.send_raw(format_line("PRIVMSG", target, line))
        return len(lines)

    # ------------------------------------------------------------------ receiving
    async def _handle(self, msg: IrcMessage) -> None:
        cmd = msg.command
        if cmd == "PING":
            await self.send_raw(format_line("PONG", msg.trailing))
        elif cmd == "CAP":
            await self._handle_cap(msg)
        elif cmd == "001":
            self.nick = msg.params[0]
            log.info("[%s] registered", self.nick)
            for channel in self.channels:
                await self.send_raw(format_line("JOIN", channel))
        elif cmd in ("432", "433"):  # erroneous nick / nick in use (only matters before registration)
            if not self.ready.is_set():
                self.nick += "_"
                log.warning("nick in use, trying %s", self.nick)
                await self.send_raw(format_line("NICK", self.nick))
        elif cmd == "JOIN" and msg.nick == self.nick:
            channel = msg.params[0].lower()
            self._joined.add(channel)
            self._join_time[channel] = datetime.now(timezone.utc)
            log.info("[%s] joined %s", self.nick, msg.params[0])
            if all(c.lower() in self._joined for c in self.channels):
                self.ready.set()
        elif cmd == "NICK" and msg.nick == self.nick:
            self.nick = msg.trailing
        elif cmd == "BATCH" and msg.params:
            ref = msg.params[0]
            if ref.startswith("+"):
                self._batches[ref[1:]] = msg.params[1] if len(msg.params) > 1 else ""
            elif ref.startswith("-"):
                self._batches.pop(ref[1:], None)
        elif cmd in ("PRIVMSG", "NOTICE") and len(msg.params) >= 2:
            await self._handle_chat(msg)
        elif cmd == "ERROR":
            raise ConnectionError(f"server error: {msg.trailing}")

    async def _handle_cap(self, msg: IrcMessage) -> None:
        sub = msg.params[1].upper() if len(msg.params) > 1 else ""
        if sub == "LS":
            offered = {c.split("=", 1)[0] for c in msg.trailing.split()}
            more = len(msg.params) > 3 and msg.params[2] == "*"
            self._caps |= offered & WANTED_CAPS
            if not more:
                if self._caps:
                    await self.send_raw(format_line("CAP", "REQ", " ".join(sorted(self._caps))))
                else:
                    await self.send_raw("CAP END")
        elif sub in ("ACK", "NAK"):
            if sub == "NAK":
                self._caps.clear()
            await self.send_raw("CAP END")

    def _is_history(self, msg: IrcMessage, target: str, sent_at: datetime | None) -> bool:
        batch = msg.tags.get("batch")
        if batch and "chathistory" in self._batches.get(batch, ""):
            return True
        joined = self._join_time.get(target.lower())
        return bool(sent_at and joined and sent_at < joined - HISTORY_SLACK)

    async def _handle_chat(self, msg: IrcMessage) -> None:
        sender = msg.nick or ""
        target, text = msg.params[0], msg.params[-1]
        if not sender or sender == self.nick or "." in sender and "!" not in (msg.prefix or ""):
            return  # own echo or server notice
        is_action = False
        if text.startswith("\x01"):
            body = text.strip("\x01")
            if body.upper().startswith("ACTION "):
                text, is_action = body[7:], True
            else:
                return  # other CTCP (VERSION, PING...) ignored
        sent_at = _parse_server_time(msg.tags.get("time"))
        is_private = not target.startswith(("#", "&"))
        event = ChatEvent(
            sender=sender,
            target=target,
            text=text,
            is_private=is_private,
            is_history=self._is_history(msg, target, sent_at),
            is_notice=msg.command == "NOTICE",
            is_action=is_action,
            time=sent_at,
        )
        try:
            await self.on_event(event)
        except Exception:  # never let a handler bug kill the connection
            log.exception("[%s] event handler failed", self.nick)
