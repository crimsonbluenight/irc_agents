"""Live test: real IRC server (localhost:6667) + real Ollama with the models from agents.yaml.

Uses a throw-away channel and a test human nick so it neither pollutes #agents nor clashes with your own nick.
Run: uv run pytest -m integration -s
"""

from __future__ import annotations

import asyncio
import socket
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from irc_agents.agent import Agent
from irc_agents.backends import BackendError, create_backend
from irc_agents.config import load_config
from irc_agents.irc.client import ChatEvent, IrcConnection

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[1]
HUMAN = "itest_human"


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


async def until(cond, timeout: float) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            return False
        await asyncio.sleep(0.2)
    return True


async def _live_app():
    app = load_config(ROOT / "agents.yaml")
    if not _port_open(app.server.host, app.server.port):
        pytest.skip(f"IRC server not running on {app.server.host}:{app.server.port}")
    channel = f"#itest-{uuid.uuid4().hex[:6]}"
    suffix = uuid.uuid4().hex[:3]
    agents_cfg = [replace(a, nick=f"{a.nick[:20]}_{suffix}") for a in app.agents]
    app = replace(app, server=replace(app.server, channel=channel), humans=[HUMAN], agents=agents_cfg)
    for cfg in app.agents:
        try:
            await create_backend(cfg.backend).check()
        except BackendError as exc:
            pytest.skip(str(exc))
    return app, channel


def _agent_turns(events, nicks) -> int:
    senders = [e.sender for e in events if e.sender in nicks]
    return sum(1 for i, s in enumerate(senders) if i == 0 or s != senders[i - 1])


async def test_live_conversation():
    app, channel = await _live_app()
    nicks = app.agent_nicks
    max_turns = app.agents[0].policy.max_agent_turns
    agents = [Agent(cfg, app, create_backend(cfg.backend)) for cfg in app.agents]
    channel_log: list[ChatEvent] = []
    private_log: list[ChatEvent] = []

    async def on_event(ev: ChatEvent):
        (private_log if ev.is_private else channel_log).append(ev)
        where = "PM" if ev.is_private else channel
        print(f"  [{where}] <{ev.sender}> {ev.text}")

    human = IrcConnection(app.server.host, app.server.port, HUMAN, [channel], on_event, password=app.server.password)
    tasks = [asyncio.create_task(a.run()) for a in agents] + [asyncio.create_task(human.run())]
    try:
        for conn in [*(a.irc for a in agents), human]:
            await asyncio.wait_for(conn.ready.wait(), 15)

        def agent_lines():
            return [e for e in channel_log if e.sender in nicks]

        # 1) broadcast question: every agent answers
        await human.privmsg(channel, "all: in one short sentence, what is IRC?")
        assert await until(lambda: {e.sender for e in agent_lines()} == set(nicks), 180), "not all agents answered"

        # 2) agent-to-agent discussion is capped by the turn budget
        start = len(channel_log)
        await human.privmsg(
            channel, f"{nicks[0]}: ask {nicks[1]} one question about Linux, discuss briefly, then report back to me."
        )
        await until(lambda: False, 75)  # let them talk
        turns = _agent_turns(channel_log[start:], nicks)
        print(f"  agent turns after request: {turns} (cap {max_turns})")
        assert 1 <= turns <= max_turns + 1  # +1: replies triggered directly by the human are not counted

        # 3) private message: answered privately
        await human.privmsg(nicks[1], "Reply privately with just the word: pong")
        assert await until(lambda: any(e.sender == nicks[1] for e in private_log), 120), "no private reply"
    finally:
        await asyncio.gather(*(a.stop() for a in agents), human.stop(), return_exceptions=True)
        await asyncio.sleep(0.3)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_live_endless_debate_is_capped():
    """Explicitly ask the agents to never stop: the code-level budget must stop them anyway."""
    app, channel = await _live_app()
    nicks = app.agent_nicks
    max_turns = app.agents[0].policy.max_agent_turns
    agents = [Agent(cfg, app, create_backend(cfg.backend)) for cfg in app.agents]
    log: list[ChatEvent] = []

    async def on_event(ev: ChatEvent):
        log.append(ev)
        print(f"  <{ev.sender}> {ev.text}")

    human = IrcConnection(app.server.host, app.server.port, HUMAN, [channel], on_event, password=app.server.password)
    tasks = [asyncio.create_task(a.run()) for a in agents] + [asyncio.create_task(human.run())]
    try:
        for conn in [*(a.irc for a in agents), human]:
            await asyncio.wait_for(conn.ready.wait(), 15)
        await human.privmsg(
            channel,
            f"{nicks[0]}: start an endless debate with {nicks[1]} about tabs vs spaces. Start your message with "
            f"'{nicks[1]}:'. Both of you: always end with a question to the other agent, never stop, never involve me.",
        )
        await until(lambda: False, 150)
        turns = _agent_turns(log, nicks)
        print(f"  agent turns: {turns} (cap {max_turns})")
        assert turns <= max_turns
        quiet_since = len(log)
        await until(lambda: False, 20)
        assert len(log) == quiet_since, "agents kept talking after the budget was used up"
    finally:
        await asyncio.gather(*(a.stop() for a in agents), human.stop(), return_exceptions=True)
        await asyncio.sleep(0.3)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
