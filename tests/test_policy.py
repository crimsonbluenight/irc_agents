import pytest

from irc_agents.config import PolicyConfig
from irc_agents.policy import ConversationPolicy, is_pass, leading_addressees, mentions


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


def make(clock, **kw):
    return ConversationPolicy("a1_qwen", ["jakub"], PolicyConfig(**kw), clock=clock, agents=["a2_llama"])


@pytest.mark.parametrize(
    "text,expected",
    [
        ("a1_qwen: hi", ["a1_qwen"]),
        ("A1_Qwen, a2_llama: hi", ["a1_qwen", "a2_llama"]),
        ("all: hi", ["all"]),
        ("@a1_qwen what now", ["a1_qwen"]),
        ("a1_qwen, can you check", ["a1_qwen"]),
        ("no address here", []),
        ("http://x.y: hi", []),
    ],
)
def test_leading_addressees(text, expected):
    assert leading_addressees(text) == expected


def test_mentions_whole_word():
    assert mentions("what does a1_qwen think?", "a1_qwen")
    assert not mentions("a1_qwen2 here", "a1_qwen")


def test_human_addressing(clock):
    p = make(clock)  # default: unaddressed human lines go to everyone
    assert p.decide_channel("jakub", "a1_qwen: hi").reply
    assert p.decide_channel("jakub", "all: hi").human_triggered
    assert p.decide_channel("jakub", "just talking to everyone").reply
    assert p.decide_channel("jakub", "Hm, what now?").reply  # "hm" is not a known nick -> unaddressed
    assert p.decide_channel("JAKUB", "what does a1_qwen think").reply
    assert not p.decide_channel("jakub", "a2_llama: only you").reply
    assert not p.decide_channel("jakub", "a2_llama: ask a1_qwen about X").reply
    assert p.decide_channel("jakub", "a1_qwen, a2_llama: both of you").reply
    assert p.decide_channel("jakub", "Hm, a1_qwen, what now?").reply


def test_human_addressing_only_when_addressed(clock):
    p = make(clock, reply_to_unaddressed_human=False)
    assert not p.decide_channel("jakub", "just talking").reply
    assert p.decide_channel("jakub", "a1_qwen: hi").reply
    assert p.decide_channel("jakub", "all: hi").reply
    assert p.decide_channel("jakub", "what does a1_qwen think").reply
    assert not p.decide_channel("jakub", "a2_llama: hi").reply


def test_agent_addressing_is_strict(clock):
    p = make(clock)
    assert p.decide_channel("a2_llama", "a1_qwen: your turn").reply
    assert not p.decide_channel("a2_llama", "all: hello agents").reply
    assert not p.decide_channel("a2_llama", "as a1_qwen said, yes").reply
    assert not p.decide_channel("a1_qwen", "a1_qwen: talking to myself").reply


def test_turn_budget_and_reset(clock):
    p = make(clock, max_agent_turns=3)
    p.observe_channel("jakub")
    for _ in range(3):
        clock.t += 10
        p.observe_channel("a2_llama")
    assert p.agent_streak == 3
    d = p.decide_channel("a2_llama", "a1_qwen: again?")
    assert not d.reply and "budget" in d.reason
    assert not p.may_send_channel(human_triggered=False)
    assert p.may_send_channel(human_triggered=True)
    p.observe_channel("jakub")
    assert p.decide_channel("a2_llama", "a1_qwen: again?").reply


def test_multiline_message_counts_as_one_turn(clock):
    p = make(clock)
    for _ in range(4):
        p.observe_channel("a2_llama")
        clock.t += 0.3
    assert p.agent_streak == 1
    clock.t += 10
    p.observe_channel("a2_llama")
    assert p.agent_streak == 2
    p.note_sent("mine", in_channel=True)
    p.note_sent("mine too", in_channel=True)
    assert p.agent_streak == 3
    p.note_sent("private", in_channel=False)
    assert p.agent_streak == 3


def test_private_only_from_humans(clock):
    p = make(clock)
    assert p.decide_private("jakub").reply
    assert not p.decide_private("a2_llama").reply


@pytest.mark.parametrize("raw", ["PASS", " pass. ", "**PASS**", "", "a1_qwen: PASS", "<a1_qwen> PASS"])
def test_clean_reply_pass(clock, raw):
    assert make(clock).clean_reply(raw) is None


def test_clean_reply_strips_own_prefix_and_truncates(clock):
    p = make(clock, max_reply_lines=2, max_reply_chars=1000)
    assert p.clean_reply("a1_qwen: hello") == "hello"
    assert p.clean_reply("<a1_qwen> hello") == "hello"
    assert p.clean_reply("a\n\nb\nc\nd") == "a\nb [...]"
    long = make(clock, max_reply_chars=20).clean_reply("word " * 50)
    assert len(long) <= 26 and long.endswith("[...]")


def test_dedupe(clock):
    p = make(clock)
    p.note_sent("The answer is 42.", in_channel=True)
    assert p.clean_reply("The answer is 42.") is None
    assert p.clean_reply("the answer is 42!") is None
    assert p.clean_reply("Something completely different") is not None


def test_cooldown(clock):
    p = make(clock, min_reply_interval=2.0)
    assert p.cooldown_remaining() == 0
    p.note_sent("x", in_channel=True)
    clock.t += 0.5
    assert p.cooldown_remaining() == pytest.approx(1.5)


def test_clean_reply_cuts_invented_speakers(clock):
    p = make(clock)
    assert p.clean_reply("My answer.\n<jakub> thanks!\n<a2_llama> me too") == "My answer."
    assert p.clean_reply("<jakub> I am pretending") is None
    p.observe_channel("random_guy")
    assert p.clean_reply("ok\n<random_guy> hi") == "ok"
    # HTML/code and markdown bullets are not speaker lines
    assert p.clean_reply("Use:\n<div>x</div>\n* item one\n* item two") == "Use:\n<div>x</div>\n* item one\n* item two"


def test_dedupe_ignores_address_and_catches_parroting(clock):
    p = make(clock)
    p.note_sent("a2_llama: The answer is 42.", in_channel=True)
    assert p.clean_reply("jakub: the answer is 42.") is None
    other = ["a1_qwen: Spaces save time in editing. What do you think?"]
    assert p.clean_reply("a2_llama: Spaces save time in editing. What do you think?", other) is None
    assert p.clean_reply("a2_llama: Tabs are better for accessibility.", other) is not None


def test_is_pass():
    assert is_pass("PASS") and is_pass(" Pass. ") and is_pass("a1_qwen: PASS") and is_pass("<x> pass")
    assert not is_pass("I pass the question to agent2") and not is_pass("")
