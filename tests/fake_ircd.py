"""Tiny in-process IRC server for tests: CAP (server-time, batch), registration, JOIN with Ergo-style history
replay in a ``chathistory`` batch, PRIVMSG relay to channels and nicks. Records every PRIVMSG it relays."""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from irc_agents.irc.protocol import format_line, parse_line


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


@dataclass
class Client:
    writer: asyncio.StreamWriter
    nick: str = "*"
    caps: set[str] = field(default_factory=set)
    registered: bool = False
    cap_negotiating: bool = False

    def send(self, line: str) -> None:
        self.writer.write((line + "\r\n").encode())


@dataclass
class Relayed:
    sender: str
    target: str
    text: str


class FakeIrcd:
    def __init__(self) -> None:
        self.clients: dict[str, Client] = {}  # lower nick -> client
        self.channels: dict[str, set[str]] = {}
        self.history: dict[str, list[tuple[datetime, str, str]]] = {}  # chan -> (time, nick, text)
        self.log: list[Relayed] = []
        self._batch_ids = itertools.count(1)
        self.server: asyncio.base_events.Server | None = None
        self.port = 0

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        for c in list(self.clients.values()):
            c.writer.close()
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    def add_history(self, channel: str, nick: str, text: str, age_s: float = 60) -> None:
        when = datetime.now(timezone.utc) - timedelta(seconds=age_s)
        self.history.setdefault(channel.lower(), []).append((when, nick, text))

    def messages(self, sender: str | None = None, target: str | None = None) -> list[Relayed]:
        return [
            m
            for m in self.log
            if (sender is None or m.sender.lower() == sender.lower())
            and (target is None or m.target.lower() == target.lower())
        ]

    # ------------------------------------------------------------------
    def _tagged(self, client: Client, line: str, when: datetime | None = None, batch: str | None = None) -> str:
        tags = []
        if "server-time" in client.caps:
            tags.append(f"time={_ts(when or datetime.now(timezone.utc))}")
        if batch and "batch" in client.caps:
            tags.append(f"batch={batch}")
        return ("@" + ";".join(tags) + " " + line) if tags else line

    def _welcome(self, c: Client) -> None:
        if c.registered or c.cap_negotiating or c.nick == "*":
            return
        c.registered = True
        c.send(f":fake.irc 001 {c.nick} :Welcome")

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        c = Client(writer)
        try:
            while raw := await reader.readline():
                msg = parse_line(raw.decode().rstrip("\r\n"))
                cmd, p = msg.command, msg.params
                if cmd == "CAP" and p and p[0] == "LS":
                    c.cap_negotiating = True
                    c.send(":fake.irc CAP * LS :server-time batch message-tags")
                elif cmd == "CAP" and p and p[0] == "REQ":
                    c.caps = set(p[-1].split())
                    c.send(f":fake.irc CAP * ACK :{p[-1]}")
                elif cmd == "CAP" and p and p[0] == "END":
                    c.cap_negotiating = False
                    self._welcome(c)
                elif cmd == "NICK":
                    if p[0].lower() in self.clients:
                        c.send(f":fake.irc 433 * {p[0]} :Nickname is already in use")
                        continue
                    self.clients.pop(c.nick.lower(), None)
                    c.nick = p[0]
                    self.clients[c.nick.lower()] = c
                elif cmd == "USER":
                    self._welcome(c)
                elif cmd == "PING":
                    c.send(f":fake.irc PONG fake.irc :{p[-1]}")
                elif cmd == "JOIN":
                    self._join(c, p[0])
                elif cmd == "PRIVMSG":
                    self._privmsg(c, p[0], p[-1])
                elif cmd == "QUIT":
                    break
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self.clients.pop(c.nick.lower(), None)
            for members in self.channels.values():
                members.discard(c.nick.lower())
            writer.close()

    def _join(self, c: Client, channel: str) -> None:
        key = channel.lower()
        self.channels.setdefault(key, set()).add(c.nick.lower())
        c.send(self._tagged(c, f":{c.nick}!u@h JOIN {channel}"))
        c.send(f":fake.irc 366 {c.nick} {channel} :End of /NAMES list.")
        hist = self.history.get(key, [])
        if hist:
            ref = f"h{next(self._batch_ids)}"
            if "batch" in c.caps:
                c.send(f":fake.irc BATCH +{ref} chathistory {channel}")
            for when, nick, text in hist:
                c.send(self._tagged(c, format_line(f":{nick}!u@h PRIVMSG", channel, text), when, ref))
            if "batch" in c.caps:
                c.send(f":fake.irc BATCH -{ref}")

    def _privmsg(self, c: Client, target: str, text: str) -> None:
        self.log.append(Relayed(c.nick, target, text))
        line = format_line(f":{c.nick}!u@h PRIVMSG", target, text)
        if target.startswith("#"):
            self.history.setdefault(target.lower(), []).append((datetime.now(timezone.utc), c.nick, text))
            recipients = [n for n in self.channels.get(target.lower(), set()) if n != c.nick.lower()]
        else:
            recipients = [target.lower()] if target.lower() in self.clients else []
        for nick in recipients:
            other = self.clients[nick]
            other.send(self._tagged(other, line))
