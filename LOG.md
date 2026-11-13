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

## 2026-11-13: Git
- `git add -A && git commit`: initial commit 7c5fde2 (code, tests, docs, uv.lock).
- `git push -u origin main`: pushed to https://github.com/crimsonbluenight/irc_agents.git

## 2026-11-13: Nicks a1_qwen / a2_llama; unaddressed lines go to every agent
- `sed` rename agent1_/agent2_ -> a1_/a2_ in agents.yaml, README, docs, CLAUDE.md, tests.
- policy: `human_addressing()` classifies a human line (me / broadcast / other / mention / none). A leading
  `nick:` limits who answers. An unaddressed line is answered by everyone (`reply_to_unaddressed_human`, now
  default true). Fixed a bug where that option would also have made agents answer lines addressed to another agent.
- Prompt explains the addressing rule; e2e test checks that non-addressed agents still see the question and answer.
- `uv run pytest -m "not integration"`: 51 passed. Restarted the agents (`pkill -INT`, `uv run irc-agents`).

## 2026-11-13: Fix: qwen silent on "hello guys" + used old nick
- Agent log: qwen replied "hello guys", and the parroting filter (meant for agent-to-agent copying) dropped it.
  Its introduction used the old nick `agent1_qwen`, copied from the replayed channel history.
- Fixes: the parroting check now compares only against other agents' live lines. Replayed history is rendered as
  `[YYYY-MM-DD HH:MM] <nick> ...` under a header ("before you joined, nicks may have changed"), followed by a
  live-conversation marker (`memory.py`).
- `scratchpad/probe.py`: live repro on a scratch channel seeded with old-nick history. qwen now greets back and
  introduces itself as a1_qwen. 53 fast tests pass. Restarted the agents.
- `git commit` + `git push`: nick rename, addressing rules, PASS/history/parroting fixes.
