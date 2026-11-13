# LOG

## 2026-11-13
- `ls -la`, `cat prompt.txt`: inspected the repo (no code yet, only the requirements brief).
- Inspected `../irc_server` (README) to learn the IRC server setup.
- Created `CLAUDE.md` with the project brief, constraints and IRC server details.

## 2026-11-13: Phase 1 implementation (Ollama PoC)
Environment checks:
- `which ollama uv docker; ollama list; nvidia-smi; free -g; docker ps`: inventory (RTX 4060 8 GB, 62 GB RAM,
  Ollama 0.18.3 with qwen3:8b, llama3.2, mistral...; Ergo running as `ircd-6667`, no server password).
- `curl localhost:11434/api/chat ...` + `/api/ps`: loading both models shows they don't fit in VRAM together;
  Ollama unloads one instead of crashing.
- `python3 scratchpad/bench.py`: measured swapping (~1.6 s per switch, qwen 47 tok/s) vs a fixed GPU/CPU split
  (`num_gpu` 24-28: both stay loaded, qwen 20-23 tok/s). Default: let Ollama swap and send requests one at a time;
  `num_gpu` is optional.
- `grep ... ../irc_server/server/ircd.yaml`: Ergo replays the last 25 channel lines on join (`autoreplay-on-join: 25`).

Project setup:
- `uv init --package --name irc-agents --python 3.10 --vcs none .`; `uv add ollama pyyaml httpx`;
  `uv add --dev pytest pytest-asyncio black`.
- Code: `src/irc_agents/{cli,config,agent,policy,memory}.py`, `irc/{protocol,client}.py`,
  `backends/{base,ollama}.py`; `agents.yaml`; `prompts/default.md`.

Testing and iteration:
- `uv run pytest -m "not integration"`: unit tests + end-to-end tests against `tests/fake_ircd.py`.
  Fixed: split_text dropped indentation; tests assumed one IRC line per reply.
- `scratchpad/rawtrace.py`: traced raw IRC with the real Ergo (CAP LS is multi-line; batch/server-time ACKed).
- `uv run pytest -m integration -s` (several runs). Behaviour fixes found live:
  - A line addressed to one agent no longer triggers other agents it merely mentions.
  - Added a reminder line so small models stop writing other speakers' lines; those lines are cut anyway.
  - Prompt: never PASS on direct questions; replies to agents are auto-addressed back to them.
  - Dedupe now also catches an agent parroting another agent's line.
  - "Endless debate" stress test: stopped after 3 turns (cap 6).
- `uv run irc-agents -c scratchpad/cli.yaml` + SIGINT: start/stop on test channel `#clitest`; error exit codes checked.
- `uv run black src tests`: formatting (line length 120).
- Docs: README.md, CLAUDE.md, docs/plan/001-architecture.md.

## 2026-11-13: Fix: qwen sometimes silent toward the human
- Read the agent log: qwen got "all: hi guys" and a later question, but the model answered `PASS`, so nothing was posted.
  `agent2_qwen:` (typo) addressed no existing agent, so nobody answered; that is expected.
- Fix: human-addressed replies get a reminder without the PASS option; if the model still says PASS it is asked
  once more ("PASS is not allowed"). Prompt rule 5 updated. Added `policy.is_pass` and tests (50 pass).
- Restarted the agents: `pkill -INT -f "bin/irc-agents$"`, then `uv run irc-agents` (background).
