# 001: Architecture of irc-agents (PoC: Ollama chat agents)

Date: 2026-11-13. Status: implemented (phase 1).

## Goals

- Start N LLM agents, each a separate IRC client in `#agents` on the local Ergo server (`../irc_server`).
- Configure in one file how many agents there are, and each agent's model and master prompt.
- The human (`jakub`) chats with the agents in the channel or by private message. Agents talk to each other only in
  the channel, never by private message.
- No endless agent-to-agent chatter.
- Phase 1 has Ollama only. Later phases add Claude chat, OpenAI chat, Claude Code and Codex, and agents that work
  together in a container on software tasks.

## Decisions agreed with the user

| Topic | Decision |
|---|---|
| Human nick | `jakub` (`humans:` list in the config; every other nick is treated as an agent) |
| Models | `agent1_qwen` = `qwen3:8b` (think off), `agent2_llama` = `llama3.2` |
| GPU (RTX 4060, 8 GB) | Ollama swaps models (~1.6 s), with requests sent one at a time; optional `num_gpu` split. See the README for measurements |
| Addressing | Agents reply only when addressed: `nick:`, `a, b:`, `all:`; nicks are not model names |
| IRC client | Own small asyncio client (no dependency; full control of CAPs, history and reconnect) |
| Language | English |

## Structure

```
cli.py           argparse, preflight (backend.check), starts agents, SIGINT/SIGTERM -> QUIT
config.py        YAML -> dataclasses; defaults deep-merged into each agent; nick validation
agent.py         Agent = IrcConnection + ConversationPolicy + Memory + Backend; one reply-worker task per agent
policy.py        all conversation rules (pure logic, unit-tested with a fake clock)
memory.py        per-conversation rolling window -> chat messages (others' lines rendered as "<nick> text")
irc/protocol.py  parsing (IRCv3 tags) / formatting / UTF-8-safe splitting
irc/client.py    connection, CAP server-time+batch, PING, nick-collision fallback, reconnect with backoff,
                 recognises replayed history (chathistory batch or server-time before the JOIN)
backends/base.py Backend ABC (check / generate / aclose) + registry (@register_backend("type"))
backends/ollama.py  ollama.AsyncClient; one semaphore per endpoint; strips <think> blocks
```

How a message flows: an IRC line arrives → `Agent.on_event` adds it to memory, updates the turn counter and asks the
policy whether to reply. The handler never blocks. If a reply is due, the conversation is queued for the worker. The
worker waits about 0.8 s so a multi-line message is read completely, builds the messages, calls the backend and
cleans the result (PASS, length, duplicates, invented speakers). It then re-checks the budget, applies the cooldown
and sends.

## Loop prevention (two layers)

1. Prompt (`prompts/default.md`): be brief, no small talk, `PASS` when there is nothing to add, hand the result
   back to the human.
2. Code (`policy.py`):
   - Agents react only to explicit `nick:` addressing from another agent.
   - A turn budget per human message (`max_agent_turns`).
   - Duplicate and parroting filter, cooldown, length caps.
   - Replies to an agent are auto-addressed to it so a real Q&A can finish.

   Live test: asking the agents for an "endless debate" stopped after 3 turns.

## Extension points for later phases

- **Chat providers (Claude, OpenAI):** write `backends/anthropic.py` or `backends/openai.py` with a
  `@register_backend("anthropic")` class implementing `generate(messages)`, and import it in `backends/__init__.py`.
  Provider-specific settings go in `backend.extra`; API keys come from environment variables. The agent, policy and
  IRC code don't change.
- **Tool agents (Claude Code, Codex):** these act instead of only chatting. The plan is a `WorkerAgent` that reuses
  `IrcConnection`, `ConversationPolicy` and `Memory`, but whose "backend" runs a CLI session in a shared workspace
  container (a Docker volume with the repo) and reports progress to `#agents`. The ability to run tools is the
  boundary between the two agent kinds. Coordination (who works on what) will need a task/lock convention in the
  channel; that is to be designed in plan 002.
- Per-agent policy is already configurable. Tool agents will likely need a larger turn budget, and only for
  task-related traffic.
