"""IRC line parsing/formatting (RFC 1459 + IRCv3 message tags)."""

from __future__ import annotations

from dataclasses import dataclass, field

# Max bytes of a full IRC line incl. CRLF is 512. Leave room for ":nick!user@host PRIVMSG #target :" added by the
# server when relaying, so the payload we send per line is kept well below that.
MAX_TEXT_BYTES = 380

_TAG_UNESCAPE = {":": ";", "s": " ", "\\": "\\", "r": "\r", "n": "\n"}


@dataclass
class IrcMessage:
    command: str
    params: list[str] = field(default_factory=list)
    prefix: str | None = None
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def nick(self) -> str | None:
        """Nick part of the prefix (``nick!user@host`` -> ``nick``)."""
        if not self.prefix:
            return None
        return self.prefix.split("!", 1)[0].split("@", 1)[0]

    @property
    def trailing(self) -> str:
        return self.params[-1] if self.params else ""


def _unescape_tag(value: str) -> str:
    out, i = [], 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            out.append(_TAG_UNESCAPE.get(value[i + 1], value[i + 1]))
            i += 2
            continue
        if ch != "\\":
            out.append(ch)
        i += 1
    return "".join(out)


def parse_line(line: str) -> IrcMessage:
    """Parse one IRC line (without CRLF)."""
    line = line.rstrip("\r\n")
    tags: dict[str, str] = {}
    prefix = None
    if line.startswith("@"):
        raw_tags, _, line = line[1:].partition(" ")
        for item in raw_tags.split(";"):
            if not item:
                continue
            key, _, value = item.partition("=")
            tags[key] = _unescape_tag(value)
        line = line.lstrip(" ")
    if line.startswith(":"):
        prefix, _, line = line[1:].partition(" ")
        line = line.lstrip(" ")
    trailing = None
    if " :" in line:
        line, trailing = line.split(" :", 1)
    elif line.startswith(":"):
        line, trailing = "", line[1:]
    parts = line.split()
    command = parts[0].upper() if parts else ""
    params = parts[1:]
    if trailing is not None:
        params.append(trailing)
    return IrcMessage(command=command, params=params, prefix=prefix, tags=tags)


def format_line(command: str, *params: str) -> str:
    """Build an IRC line (without CRLF). The last param is sent as trailing when needed."""
    clean = [p.replace("\r", " ").replace("\n", " ") for p in params]
    if clean and (not clean[-1] or " " in clean[-1] or clean[-1].startswith(":")):
        clean[-1] = ":" + clean[-1]
    return " ".join([command, *clean])


def split_text(text: str, max_bytes: int = MAX_TEXT_BYTES) -> list[str]:
    """Split text into IRC-sized lines: on newlines first, then on word boundaries by UTF-8 byte length."""
    lines: list[str] = []
    for raw in text.splitlines():
        raw = raw.rstrip()
        if not raw.strip():
            continue
        stripped = raw.lstrip()
        indent = raw[: len(raw) - len(stripped)]  # keep indentation (code snippets)
        current = ""
        for i, word in enumerate(stripped.split(" ")):
            candidate = f"{current} {word}" if current else (indent + word if i == 0 else word)
            if len(candidate.encode()) <= max_bytes:
                current = candidate
                continue
            if current:
                lines.append(current)
            # Hard-split a single word longer than the limit.
            while len(word.encode()) > max_bytes:
                cut = max_bytes
                while len(word[:cut].encode()) > max_bytes:
                    cut -= 1
                lines.append(word[:cut])
                word = word[cut:]
            current = word
        if current:
            lines.append(current)
    return lines
