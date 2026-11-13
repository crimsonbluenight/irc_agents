from datetime import datetime, timezone
from pathlib import Path

import pytest

from irc_agents.agent import render_prompt
from irc_agents.config import ConfigError, load_config, parse_config
from irc_agents.memory import HISTORY_HEADER, LIVE_HEADER, Line, Memory

ROOT = Path(__file__).resolve().parents[1]


def base_raw(**over):
    raw = {
        "humans": ["jakub"],
        "defaults": {"prompt": "prompts/default.md", "backend": {"type": "ollama", "options": {"num_ctx": 4096}}},
        "agents": [
            {"nick": "a1", "backend": {"model": "m1", "options": {"temperature": 0.1}}},
            {"nick": "a2", "backend": {"model": "m2"}, "policy": {"max_agent_turns": 2}},
        ],
    }
    raw.update(over)
    return raw


def test_shipped_config_loads():
    app = load_config(ROOT / "agents.yaml")
    assert app.humans == ["jakub"]
    assert len(app.agents) == 2
    assert len({a.backend.model for a in app.agents}) == 2


def test_defaults_deep_merge():
    app = parse_config(base_raw(), base_dir=ROOT)
    a1, a2 = app.agents
    assert a1.backend.options == {"num_ctx": 4096, "temperature": 0.1}
    assert a2.backend.options == {"num_ctx": 4096}
    assert a2.policy.max_agent_turns == 2 and a1.policy.max_agent_turns == 6
    assert a1.prompt == ROOT / "prompts/default.md"


@pytest.mark.parametrize(
    "mutate,msg",
    [
        (lambda r: r["agents"].append({"nick": "A1", "backend": {"model": "x"}}), "duplicate"),
        (lambda r: r["agents"].append({"nick": "jakub", "backend": {"model": "x"}}), "collides"),
        (lambda r: r["agents"].append({"nick": "all", "backend": {"model": "x"}}), "reserved"),
        (lambda r: r["agents"].append({"nick": "1bad", "backend": {"model": "x"}}), "invalid"),
        (lambda r: r["agents"].append({"nick": "a3", "backend": {}}), "model"),
        (lambda r: r["agents"].append({"nick": "a3", "backend": {"model": "x", "bogus": 1}}), "unknown"),
        (lambda r: r.update(humans=[]), "humans"),
    ],
)
def test_validation(mutate, msg):
    raw = base_raw()
    mutate(raw)
    with pytest.raises(ConfigError, match=msg):
        parse_config(raw, base_dir=ROOT)


def test_disabled_agents_are_skipped():
    raw = base_raw()
    raw["agents"][1]["enabled"] = False
    assert [a.nick for a in parse_config(raw, base_dir=ROOT).agents] == ["a1"]


def test_render_prompt():
    app = parse_config(base_raw(), base_dir=ROOT)
    app.agents[0].persona = "Be grumpy."
    text = render_prompt(app.agents[0].prompt.read_text(), app.agents[0], app)
    assert "You are a1" in text and "a2: what do you think" in text
    assert "- a1  <- you" in text and "{" not in text.replace("{|}", "")
    assert text.rstrip().endswith("Be grumpy.")


def test_memory_to_messages():
    m = Memory(max_lines=4)
    for line in [Line("x", "old"), Line("jakub", "q1"), Line("a2", "hm", action=True), Line("me", "ans", own=True)]:
        m.add("#c", line)
    m.add("#c", Line("jakub", "q2"))
    msgs = m.to_messages("#c", "SYS")
    assert [(x.role, x.content) for x in msgs] == [
        ("system", "SYS"),
        ("user", "<jakub> q1\n* a2 hm"),
        ("assistant", "ans"),
        ("user", "<jakub> q2"),
    ]


def test_memory_marks_replayed_history():
    m = Memory(max_lines=10)
    t = datetime(2026, 11, 13, 0, 9, tzinfo=timezone.utc)
    m.add("#c", Line("jakub", "HI", history=True, time=t))
    m.add("#c", Line("agent1_qwen", "I'm agent1_qwen", history=True, time=t))
    m.add("#c", Line("jakub", "hello guys"))
    user = m.to_messages("#c", "SYS")[1].content.splitlines()
    stamp = t.astimezone().strftime("%Y-%m-%d %H:%M")
    assert user == [
        HISTORY_HEADER,
        f"[{stamp}] <jakub> HI",
        f"[{stamp}] <agent1_qwen> I'm agent1_qwen",
        LIVE_HEADER,
        "<jakub> hello guys",
    ]
