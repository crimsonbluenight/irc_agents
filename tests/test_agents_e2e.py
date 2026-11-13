"""Agents + real IRC client code against the in-process fake server, with scripted (non-LLM) backends."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable, Sequence

import pytest

from irc_agents import agent as agent_mod
from irc_agents.agent import Agent
from irc_agents.backends.base import Backend, ChatMessage
from irc_agents.config import BackendConfig, parse_config
from irc_agents.irc.client import ChatEvent, IrcConnection

from .fake_ircd import FakeIrcd

ROOT = Path(__file__).resolve().parents[1]
CHAN = "#agents"


class ScriptedBackend(Backend):
    def __init__(self, fn: Callable[[Sequence[ChatMessage]], str]) -> None:
        super().__init__(BackendConfig(type="scripted", model="script"))
        self.fn = fn
        self.calls: list[Sequence[ChatMessage]] = []

    async def generate(self, messages):
        self.calls.append(messages)
        await asyncio.sleep(0.01)
        return self.fn(messages)


async def until(cond: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(agent_mod, "DEBOUNCE", 0.05)


@pytest.fixture
async def ircd():
    server = FakeIrcd()
    await server.start()
    yield server
    await server.close()


class Harness:
    def __init__(self, ircd: FakeIrcd):
        self.ircd = ircd
        self.tasks: list[asyncio.Task] = []
        self.agents: list[Agent] = []
        self.conns: list[IrcConnection] = []

    async def start_agents(self, fns: dict[str, Callable], **policy) -> list[Agent]:
        raw = {
            "server": {"host": "127.0.0.1", "port": self.ircd.port, "channel": CHAN},
            "humans": ["jakub"],
            "defaults": {"prompt": "prompts/default.md", "policy": {"min_reply_interval": 0, **policy}},
            "agents": [{"nick": nick, "backend": {"model": "script"}} for nick in fns],
        }
        app = parse_config(raw, base_dir=ROOT)
        for cfg in app.agents:
            a = Agent(cfg, app, ScriptedBackend(fns[cfg.nick]))
            a.irc.line_delay = 0.01
            self.agents.append(a)
            self.tasks.append(asyncio.create_task(a.run()))
        for a in self.agents:
            await asyncio.wait_for(a.irc.ready.wait(), 5)
        return self.agents

    async def connect(self, nick: str) -> tuple[IrcConnection, list[ChatEvent]]:
        events: list[ChatEvent] = []

        async def on_event(ev):
            events.append(ev)

        conn = IrcConnection("127.0.0.1", self.ircd.port, nick, [CHAN], on_event, line_delay=0.01)
        self.conns.append(conn)
        self.tasks.append(asyncio.create_task(conn.run()))
        await asyncio.wait_for(conn.ready.wait(), 5)
        return conn, events

    async def close(self):
        for x in [*self.agents, *self.conns]:
            await x.stop()
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


@pytest.fixture
async def h(ircd):
    harness = Harness(ircd)
    yield harness
    await harness.close()


def last_user_text(messages: Sequence[ChatMessage]) -> str:
    """Last user turn without the trailing reminder the agent appends."""
    return [m.content for m in messages if m.role == "user"][-1].split("\n\n[You are", 1)[0]


async def test_addressing_all_and_single(h, ircd):
    await h.start_agents({"a1": lambda m: "a1 re: " + last_user_text(m), "a2": lambda m: "a2 re: " + last_user_text(m)})
    human, _ = await h.connect("jakub")

    await human.privmsg(CHAN, "all: hello")
    await until(lambda: len(ircd.messages(target=CHAN)) == 3)
    assert {m.text for m in ircd.messages(target=CHAN)[1:]} == {
        "a1 re: <jakub> all: hello",
        "a2 re: <jakub> all: hello",
    }

    await human.privmsg(CHAN, "a2: only you")
    await until(lambda: len(ircd.messages(sender="a2")) == 2)
    await human.privmsg(CHAN, "nobody addressed")
    await asyncio.sleep(0.3)
    assert len(ircd.messages(sender="a1")) == 1 and len(ircd.messages(sender="a2")) == 2
    assert ircd.messages(sender="a2")[-1].text == "a2 re: <a1> a1 re: <jakub> all: hello"  # invented lines cut


async def test_agent_loop_is_capped_and_human_resets(h, ircd):
    counter = {"n": 0}
    topics = [
        "cats",
        "rust compilers",
        "the weather on Mars",
        "jazz in 1959",
        "sorting algorithms",
        "tea",
        "bridges",
        "volcanoes",
        "chess openings",
        "linux kernels",
        "origami",
    ]

    def ping_pong(other):
        def fn(messages):
            counter["n"] += 1
            return f"{other}: what do you know about {topics[counter['n']]}?"

        return fn

    await h.start_agents({"a1": ping_pong("a2"), "a2": ping_pong("a1")}, max_agent_turns=4)
    human, _ = await h.connect("jakub")

    await human.privmsg(CHAN, "a1: start a discussion with a2")
    await until(lambda: len(ircd.messages(target=CHAN)) >= 5)
    await asyncio.sleep(0.6)
    agent_msgs = [m for m in ircd.messages(target=CHAN) if m.sender != "jakub"]
    assert len(agent_msgs) == 4  # capped by max_agent_turns

    await human.privmsg(CHAN, "a1: continue")
    await until(lambda: len([m for m in ircd.messages(target=CHAN) if m.sender != "jakub"]) == 8)
    await asyncio.sleep(0.6)
    assert len([m for m in ircd.messages(target=CHAN) if m.sender != "jakub"]) == 8


async def test_pass_and_duplicates_are_silent(h, ircd):
    # A human gets one retry after PASS; PASS twice -> silence. Duplicates are dropped.
    replies = iter(["PASS", "PASS", "same answer", "same answer"])
    await h.start_agents({"a1": lambda m: next(replies)})
    human, _ = await h.connect("jakub")
    for i in range(3):
        await human.privmsg(CHAN, f"a1: question {i}")
        await asyncio.sleep(0.3)
    assert [m.text for m in ircd.messages(sender="a1")] == ["same answer"]


async def test_pass_to_human_is_retried_once(h, ircd):
    calls = []

    def fn(messages):
        calls.append(messages)
        return "PASS" if len(calls) == 1 else "hello jakub"

    await h.start_agents({"a1": fn})
    human, _ = await h.connect("jakub")
    await human.privmsg(CHAN, "all: hi guys")
    await until(lambda: ircd.messages(sender="a1"))
    assert ircd.messages(sender="a1")[0].text == "hello jakub"
    assert "do not reply PASS" in calls[0][-1].content
    assert "PASS is not allowed" in calls[1][-1].content


async def test_private_messages(h, ircd):
    (a1,) = await h.start_agents({"a1": lambda m: "private reply: " + last_user_text(m)})
    human, human_events = await h.connect("jakub")
    intruder, _ = await h.connect("evil_agent")

    await intruder.privmsg("a1", "secret agent-to-agent chat")
    await human.privmsg("a1", "hi in private")
    await until(lambda: any(e.is_private for e in human_events))
    await asyncio.sleep(0.3)

    replies = ircd.messages(sender="a1")
    assert [r.target for r in replies] == ["jakub"]
    assert "<jakub> hi in private" in replies[0].text
    assert "secret" not in replies[0].text
    assert not ircd.messages(sender="a1", target="evil_agent")
    # PM context did not leak into the channel conversation
    assert not a1.memory.lines(CHAN)


async def test_agent_reply_never_pms_other_agents(h, ircd):
    await h.start_agents({"a1": lambda m: "a2: please reply privately", "a2": lambda m: "ok"})
    human, _ = await h.connect("jakub")
    await human.privmsg(CHAN, "a1: ask a2")
    await until(lambda: ircd.messages(sender="a2"))
    assert all(m.target == CHAN for m in ircd.log if m.sender in ("a1", "a2"))


async def test_backlog_is_context_not_trigger(h, ircd):
    ircd.add_history(CHAN, "jakub", "all: old question from yesterday")
    seen = []
    (a1,) = await h.start_agents({"a1": lambda m: seen.append(m) or "fresh answer"})
    await asyncio.sleep(0.4)
    assert not ircd.messages(sender="a1")  # history not answered
    assert [ln.text for ln in a1.memory.lines(CHAN)] == ["all: old question from yesterday"]

    human, _ = await h.connect("jakub")
    await human.privmsg(CHAN, "a1: new question")
    await until(lambda: ircd.messages(sender="a1"))
    assert "old question" in seen[0][-1].content and "new question" in seen[0][-1].content


async def test_multiline_human_message_gives_one_reply(h, ircd):
    backend_calls = []
    await h.start_agents({"a1": lambda m: backend_calls.append(m) or f"answer {len(backend_calls)}"})
    human, _ = await h.connect("jakub")
    await human.privmsg(CHAN, "a1: line one\na1: line two\na1: line three")
    await until(lambda: ircd.messages(sender="a1"))
    await asyncio.sleep(0.4)
    assert len(backend_calls) == 1
    assert "line three" in backend_calls[0][-1].content


async def test_reply_to_agent_is_addressed_back(h, ircd):
    await h.start_agents({"a1": lambda m: "a2: what is 2+2?", "a2": lambda m: "It is 4."}, max_agent_turns=2)
    human, _ = await h.connect("jakub")
    await human.privmsg(CHAN, "a1: ask a2 something")
    await until(lambda: ircd.messages(sender="a2"))
    assert ircd.messages(sender="a2")[0].text == "a1: It is 4."
