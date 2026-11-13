# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

LLM agents that each connect as a separate IRC user to `#agents` on the local Ergo server from the sibling repo
`../irc_server`. Phase 1 (implemented) has local Ollama chat agents only. Later phases add Claude chat, OpenAI chat,
Claude Code and Codex, and agents collaborating on software tasks in a container. The original brief is in
`prompt.txt`, and the architecture and extension plan are in `docs/plan/001-architecture.md`.

## Commands

```bash
uv sync
uv run irc-agents --check                 # validate agents.yaml + Ollama/models
uv run irc-agents [-c agents.yaml] [--only NICK] [-v]
uv run irc-agents --show-prompt NICK      # rendered master prompt
uv run pytest -m "not integration"        # fast suite (fake in-process IRC server, scripted backends)
uv run pytest tests/test_policy.py::test_turn_budget_and_reset
uv run pytest -m integration -s           # live: needs ircd on localhost:6667 + Ollama; ~5 min
uv run black src tests                    # line length 120 (pyproject)
```

The IRC server is managed with `../irc_server/scripts/ircctl.sh start|stop|logs <port>`, and its container is
`ircd-<port>`. It has no server password by default. All server settings (password, replay length, line limits) are in `../irc_server/config.json`, documented in `../irc_server/docs/parameters.md`.

## Architecture (the parts that span several files)

- `agent.py` connects `irc/client.py` (I/O), `policy.py` (whether to reply and what may be sent), `memory.py`
  (context) and a `backends/` implementation. `on_event` must stay non-blocking: it only records, decides and queues.
  A single worker task per agent generates replies. Triggers for one conversation are merged while it is busy, after
  a short debounce (`DEBOUNCE`).
- **Loop prevention is mainly code, not prompt** (`policy.py`):
  - The turn budget counts agent *turns* since the last human line; consecutive lines of one speaker within
    `TURN_GAP` are one turn.
  - Who answers a human line: with a leading `nick:` only those agents, otherwise every agent (`reply_to_unaddressed_human`, default on). Agents react to agents only on an explicit leading `nick:`.
  - Replies to agents are auto-prefixed with the target nick.
  - The dedupe filter covers own lines and parroting of others' lines.
  - Replies are cut where the model writes lines for known speakers.
  - When changing conversation behaviour, update `prompts/default.md`, the reminder in `Agent._reply`, the policy,
    and the tests together.
- Humans are the `humans:` list. **Every other nick is treated as an agent.** Private messages are answered only for
  humans, and agents only ever send private messages to humans.
- Replayed history on JOIN is detected in `irc/client.py`, by the `chathistory` batch tag or a server-time older
  than the join. It is fed to memory as context and never answered. Don't break this: Ergo replays the last `history.replay_on_join` (default 25) items on join.
- Backends register via `@register_backend("<type>")` and must be imported in `backends/__init__.py`. New providers
  only implement `generate(messages) -> str` (+ optional `check`).
- Ollama requests are sent one at a time per endpoint (a module-level semaphore). On the 8 GB RTX 4060 the two
  default models don't fit in VRAM together, so Ollama swaps them (~1.6 s); see the README's GPU section.

## Tests

- `tests/fake_ircd.py` is an in-process IRC server that mimics Ergo (CAP, `chathistory` batch replay) and records
  every relayed PRIVMSG in `.log`. The end-to-end tests use it with `ScriptedBackend`. Scripted replies must differ
  from each other, or the dedupe filter drops them.
- The live tests (`-m integration`) use a random `#itest-*` channel and nick `itest_human`, so they never touch
  `#agents` or the user's nick `jakub`.
