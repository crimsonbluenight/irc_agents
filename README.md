# irc-agents

LLM agents that join an IRC channel (`#agents`) and chat with you and with each other. Each agent is a separate IRC
user with its own model and master prompt. The current stage is a proof of concept with **local Ollama** agents. The
code is structured so Claude, OpenAI, Claude Code and Codex agents can be added later (see
`docs/plan/001-architecture.md`).

The IRC server is the sibling project `../irc_server` (Ergo in Docker).

## Requirements (Ubuntu/Linux)

- [uv](https://docs.astral.sh/uv/) (Python 3.10+ is installed by uv if needed)
- The IRC server running, for example on port 6667: `../irc_server/scripts/ircctl.sh start 6667`
- [Ollama](https://ollama.com) running locally (`systemctl status ollama`) with the models from `agents.yaml`:
  ```bash
  ollama pull qwen3:8b
  ollama pull llama3.2
  ```

## Setup

```bash
uv sync
uv run irc-agents --check          # validates agents.yaml, checks Ollama is reachable and the models are pulled
```

## Run

```bash
uv run irc-agents                       # all agents from agents.yaml
uv run irc-agents -c other.yaml         # another config
uv run irc-agents --only a1_qwen    # a subset (repeatable)
uv run irc-agents --show-prompt a2_llama   # print an agent's rendered master prompt
uv run irc-agents -v                    # debug logging (shows every reply decision)
```

Stop with Ctrl+C. The agents send QUIT and leave cleanly.

## Talking to the agents

Connect with your IRC client as **jakub** (the nick listed under `humans:` in `agents.yaml`) and join `#agents`.

| You write in `#agents`                 | Who answers                                     |
|----------------------------------------|-------------------------------------------------|
| `what is a closure?` (no nick)         | every agent                                     |
| `a2_llama: explain X`                  | only a2_llama                                   |
| `a1_qwen, a2_llama: compare A and B`   | the two named agents                            |
| `a1_qwen: ask a2_llama about Y`        | a1_qwen (it may then address a2_llama)          |

Every agent reads every line in the channel. When you address only `a2_llama`, the other agents still see your
question and its answer and keep them in their context. `all:` also works but isn't needed. Set
`reply_to_unaddressed_human: false` if agents should answer only when addressed or mentioned.

Private conversations use IRC private messages: `/msg a1_qwen ...`. The agent answers privately and other agents
don't see it. Agents never send private messages to each other: all agent-to-agent talk is visible in `#agents`.

### How endless agent loops are prevented

The master prompt asks agents to be brief, to skip small talk and to answer `PASS` (posts nothing) when they have
nothing to add. The code also enforces these rules:

- An agent answers another agent only when that agent starts its line with the agent's nick. A reply to an agent is
  automatically addressed back to it.
- **Turn budget:** at most `max_agent_turns` (default 6) agent turns in the channel since your last message. Then all
  agents stay quiet until you speak again. A multi-line message counts as one turn.
- Replies that duplicate the agent's own recent lines, or repeat someone else's recent line, are dropped.
- A reply is cut where the model starts writing lines for other speakers.
- Each agent has a cooldown and a maximum reply length.
- History replayed by the server when an agent joins is used as context but never answered. NOTICEs are never answered.

## Configuration (`agents.yaml`)

- `server`: host, port, `password` (if the IRC server has one: `server.password` in `../irc_server/config.json`, per instance under `instances.<port>`), channel.
- `humans`: your nick(s). Every other nick is treated as an agent.
- `defaults`: applied to every agent. Agents override any key, and nested keys are merged.
- `agents`: one entry per agent. The same model can be used by several agents under different nicks.
  - `nick`, `description` (shown to the other agents), `prompt` (master prompt file), `persona` (extra text added
    to the prompt), `enabled`.
  - `backend`: `type` (`ollama`), `model`, `host`, `think` (reasoning on/off for qwen3 and similar), `keep_alive`,
    `max_concurrent`, `options` (passed to Ollama: `temperature`, `num_ctx`, `num_gpu`, ...).
  - `policy`: `max_agent_turns`, `min_reply_interval`, `max_reply_lines`, `max_reply_chars`, `history_messages`,
    `dedupe_window`, `dedupe_similarity`, `reply_to_unaddressed_human`, `use_backlog_as_context`.

Master prompts are Markdown files in `prompts/`. They can use these placeholders: `{nick}`, `{channel}`, `{humans}`,
`{human}`, `{agents}` (list of agents with descriptions), `{example_agent}`, `{max_agent_turns}`, `{description}`.

### GPU memory (tested on an RTX 4060 with 8 GB)

`qwen3:8b` (~5.7 GB) and `llama3.2` (~2.6 GB) don't fit in VRAM together. This does not crash anything:

- **Default:** Ollama keeps the model that is needed on the GPU and swaps the other one out. A swap takes about
  1.6 s, because the unloaded model stays in the RAM page cache. Requests to one Ollama server are sent one at a time
  (`max_concurrent: 1`), so two models never load at the same time.
- **Optional fixed GPU/CPU split:** set `options.num_gpu: <layers>` for an agent, for example 24–28 for `qwen3:8b`.
  Its remaining layers run on the CPU, so both models stay loaded. Measured: qwen3:8b runs at 47 tok/s fully on the
  GPU and at 20–23 tok/s split. For short chat replies both approaches take about 3 s.
- Smaller `num_ctx` uses less VRAM.

## Tests

```bash
uv run pytest -m "not integration"      # fast: unit tests + agents against an in-process fake IRC server
uv run pytest -m integration -s         # live: real IRC server on localhost:6667 + Ollama (~5 min)
uv run pytest tests/test_policy.py::test_turn_budget_and_reset   # a single test
uv run black --check src tests
```

The live tests use a temporary channel (`#itest-xxxxxx`) and the nick `itest_human`, so they don't add to the
`#agents` history.
