"""An agent = one IRC identity + conversation policy + memory + LLM backend.

Incoming lines are handled synchronously (store in memory, decide); replies are produced by one worker task per
agent so a slow model never blocks the IRC connection, and several triggers for the same conversation that arrive
while the model is busy are coalesced into one reply.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from .backends.base import Backend, BackendError
from .config import AgentConfig, AppConfig
from .irc.client import ChatEvent, IrcConnection
from .memory import Line, Memory
from .backends.base import ChatMessage
from .policy import ConversationPolicy, is_pass, leading_addressees

log = logging.getLogger(__name__)

PM_PREFIX = "pm:"
DEBOUNCE = 0.8  # seconds to wait after a trigger so multi-line messages are read completely before replying


@dataclass
class _Pending:
    human_triggered: bool
    reply_to: str | None = None  # agent whose line triggered the reply (channel only)


def render_prompt(template: str, agent: AgentConfig, app: AppConfig) -> str:
    """Fill ``{placeholders}`` in a master prompt. Unknown braces are left alone."""
    others = [a for a in app.agents if a.nick != agent.nick]
    roster_lines = [
        f"  - {a.nick}" + (f": {a.description}" if a.description else "") + ("  <- you" if a is agent else "")
        for a in app.agents
    ]
    values = {
        "nick": agent.nick,
        "channel": app.server.channel,
        "humans": ", ".join(app.humans),
        "human": app.humans[0],
        "agents": "\n".join(roster_lines),
        "example_agent": others[0].nick if others else "other_agent",
        "max_agent_turns": str(agent.policy.max_agent_turns),
        "description": agent.description,
    }
    text = template
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    if agent.persona.strip():
        text += "\n\n# Your persona\n" + agent.persona.strip() + "\n"
    return text


class Agent:
    def __init__(self, config: AgentConfig, app: AppConfig, backend: Backend) -> None:
        self.config = config
        self.app = app
        self.backend = backend
        self.channel = app.server.channel
        self.policy = ConversationPolicy(config.nick, app.humans, config.policy, agents=app.agent_nicks)
        self.memory = Memory(config.policy.history_messages)
        self.system_prompt = render_prompt(config.prompt.read_text(), config, app)
        self.irc = IrcConnection(
            host=app.server.host,
            port=app.server.port,
            nick=config.nick,
            channels=[self.channel],
            on_event=self.on_event,
            realname=f"{backend.label} agent",
            password=app.server.password,
        )
        self._pending: dict[str, _Pending] = {}
        self._wakeup = asyncio.Event()
        self.log = logging.getLogger(f"agent.{config.nick}")

    @property
    def nick(self) -> str:
        return self.config.nick

    async def run(self) -> None:
        worker = asyncio.create_task(self._worker(), name=f"{self.nick}-worker")
        try:
            await self.irc.run()
        finally:
            worker.cancel()

    async def stop(self) -> None:
        await self.irc.stop("agent shutting down")
        await self.backend.aclose()

    # ---------------------------------------------------------------- incoming
    async def on_event(self, ev: ChatEvent) -> None:
        if ev.is_notice:
            return  # never answer NOTICEs (IRC convention; prevents bot-to-bot loops)
        line = Line(ev.sender, ev.text, action=ev.is_action)
        if ev.is_private:
            decision = self.policy.decide_private(ev.sender)
            if not decision.reply:
                self.log.warning("ignored private message from %s: %s", ev.sender, decision.reason)
                return
            if ev.is_history:
                return
            key = PM_PREFIX + ev.sender
            self.memory.add(key, line)
            self._schedule(key, human_triggered=True)
            return

        if ev.target.lower() != self.channel.lower():
            return
        if ev.is_history:
            self.policy.known_speakers.add(ev.sender.lower())
            if self.config.policy.use_backlog_as_context:
                self.memory.add(self.channel, Line(ev.sender, ev.text, action=ev.is_action, history=True, time=ev.time))
            return
        self.memory.add(self.channel, line)
        self.policy.observe_channel(ev.sender)
        decision = self.policy.decide_channel(ev.sender, ev.text)
        self.log.debug("<%s> %s -> %s (%s)", ev.sender, ev.text[:80], decision.reply, decision.reason)
        if decision.reply:
            self._schedule(self.channel, decision.human_triggered, None if decision.human_triggered else ev.sender)
        elif "budget" in decision.reason:
            self.log.info("not replying to %s: %s", ev.sender, decision.reason)

    def _schedule(self, key: str, human_triggered: bool, reply_to: str | None = None) -> None:
        current = self._pending.get(key)
        self._pending[key] = _Pending(
            human_triggered or (current.human_triggered if current else False),
            reply_to or (current.reply_to if current else None),
        )
        self._wakeup.set()

    # ---------------------------------------------------------------- replying
    async def _worker(self) -> None:
        while True:
            await self._wakeup.wait()
            await asyncio.sleep(DEBOUNCE)
            self._wakeup.clear()
            while self._pending:
                key = next(iter(self._pending))
                pending = self._pending.pop(key)
                try:
                    await self._reply(key, pending.human_triggered, pending.reply_to)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.log.exception("reply failed")

    async def _reply(self, key: str, human_triggered: bool, reply_to: str | None = None) -> None:
        is_pm = key.startswith(PM_PREFIX)
        target = key[len(PM_PREFIX) :] if is_pm else self.channel
        prompt = self.system_prompt
        if is_pm:
            prompt += (
                f"\n\n# Current conversation\nThis is a PRIVATE conversation with the human {target}. "
                "Only you and they can see it. Do not address other agents here.\n"
            )
        reminder = (
            f"[You are {self.nick}. Write only your own next message as plain text (no <nick> prefixes, no lines for "
            "others). "
            + (
                "A human addressed you: answer them, do not reply PASS.]"
                if human_triggered
                else "Answer direct questions; otherwise reply exactly PASS if you have nothing useful to add.]"
            )
        )
        messages = self.memory.to_messages(key, prompt, reminder)
        self.log.info("generating reply for %s (%d context messages)", target, len(messages) - 1)
        try:
            raw = await self.backend.generate(messages)
            if human_triggered and is_pass(raw):
                # Humans always get an answer: small models sometimes PASS anyway, so ask once more.
                self.log.info("model replied PASS to a human; asking again")
                retry = "[A human addressed you directly. PASS is not allowed here. Answer briefly.]"
                raw = await self.backend.generate(
                    [*messages, ChatMessage("assistant", raw), ChatMessage("user", retry)]
                )
        except BackendError as exc:
            self.log.error("%s", exc)
            if human_triggered:
                await self._send(target, f"[error: {exc}]", is_pm, record=False)
            return
        # Parroting check only against other agents' live lines (greeting a human back with the same words is fine).
        recent_others = [
            ln.text
            for ln in self.memory.lines(key)
            if not ln.own and not ln.history and not self.policy.is_human(ln.sender)
        ]
        text = self.policy.clean_reply(raw, recent_others)
        if text is None:
            self.log.info("staying silent (PASS/empty/duplicate): %r", raw[:120])
            return
        if reply_to and not is_pm and not leading_addressees(text):
            text = f"{reply_to}: {text}"  # a reply to an agent is addressed to it, so the conversation can continue
        if not is_pm and not self.policy.may_send_channel(human_triggered):
            self.log.info("dropping reply: agent-turn budget used up while generating")
            return
        wait = self.policy.cooldown_remaining()
        if wait:
            await asyncio.sleep(wait)
        await self._send(target, text, is_pm)

    async def _send(self, target: str, text: str, is_pm: bool, record: bool = True) -> None:
        await self.irc.ready.wait()
        await self.irc.privmsg(target, text)
        self.log.info("-> %s: %s", target, text.replace("\n", " | ")[:200])
        if record:
            self.memory.add(PM_PREFIX + target if is_pm else self.channel, Line(self.nick, text, own=True))
            self.policy.note_sent(text, in_channel=not is_pm)
