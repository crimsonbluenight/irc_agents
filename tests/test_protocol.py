from irc_agents.irc.protocol import format_line, parse_line, split_text


def test_parse_full_line_with_tags():
    msg = parse_line("@time=2026-11-13T10:00:00.000Z;batch=abc :jakub!j@host PRIVMSG #agents :all: hello there\r\n")
    assert msg.tags == {"time": "2026-11-13T10:00:00.000Z", "batch": "abc"}
    assert msg.prefix == "jakub!j@host"
    assert msg.nick == "jakub"
    assert msg.command == "PRIVMSG"
    assert msg.params == ["#agents", "all: hello there"]


def test_parse_without_prefix_and_tag_escapes():
    assert parse_line("PING :abc").params == ["abc"]
    assert parse_line("@k=a\\sb\\:c CMD x").tags["k"] == "a b;c"
    assert parse_line(":srv 001 nick :Welcome").params == ["nick", "Welcome"]


def test_format_line_trailing_and_newlines():
    assert format_line("PRIVMSG", "#a", "hi there") == "PRIVMSG #a :hi there"
    assert format_line("JOIN", "#a") == "JOIN #a"
    assert format_line("PRIVMSG", "#a", ":)") == "PRIVMSG #a ::)"
    assert "\n" not in format_line("PRIVMSG", "#a", "x\ny")


def test_split_text_lines_and_bytes():
    assert split_text("a\n\n b \nc") == ["a", " b", "c"]
    parts = split_text("ż" * 300, max_bytes=100)  # 2 bytes per char
    assert all(len(p.encode()) <= 100 for p in parts) and "".join(parts) == "ż" * 300
    words = split_text(" ".join(["word"] * 100), max_bytes=50)
    assert all(len(p.encode()) <= 50 for p in words) and " ".join(words) == " ".join(["word"] * 100)
