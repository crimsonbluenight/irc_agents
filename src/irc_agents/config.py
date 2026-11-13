"""Configuration loading: one YAML file describes the server, the humans and every agent."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

NICK_RE = re.compile(r"^[A-Za-z\[\]\\`_^{|}][A-Za-z0-9\[\]\\`_^{|}-]{0,29}$")
RESERVED_NICKS = {"all", "everyone", "nickserv", "chanserv"}


class ConfigError(ValueError):
    pass


@dataclass
class ServerConfig:
    host: str = "localhost"
    port: int = 6667
    password: str | None = None
    channel: str = "#agents"


@dataclass
class BackendConfig:
    """Backend selection + parameters. ``type`` picks the implementation from the backend registry."""

    type: str = "ollama"
    model: str = ""
    host: str | None = None  # backend endpoint, e.g. http://localhost:11434 for Ollama
    options: dict[str, Any] = field(default_factory=dict)  # passed to the model (temperature, num_ctx, num_gpu...)
    think: bool | None = False  # reasoning models (qwen3...): False = answer directly, faster
    keep_alive: str | None = "30m"  # how long Ollama keeps the model loaded after a request
    max_concurrent: int = 1  # max parallel requests per backend endpoint (shared by all agents on it)
    timeout: float = 300.0
    extra: dict[str, Any] = field(default_factory=dict)  # backend-specific settings for future backends


@dataclass
class PolicyConfig:
    """Conversation rules enforced in code (the master prompt asks for the same, this guarantees it)."""

    max_agent_turns: int = 6  # agent messages allowed in the channel since the last human message
    min_reply_interval: float = 2.0  # seconds between two messages of the same agent
    max_reply_lines: int = 8
    max_reply_chars: int = 1500
    history_messages: int = 40  # lines of conversation kept as model context
    dedupe_window: int = 10  # compare a new reply against this many own recent replies
    dedupe_similarity: float = 0.9  # 0..1, replies at least this similar to a recent one are dropped
    reply_to_unaddressed_human: bool = False  # True: answer every human line in the channel, not only addressed
    use_backlog_as_context: bool = True  # replayed history on join is read as context (never answered)


@dataclass
class AgentConfig:
    nick: str
    backend: BackendConfig
    prompt: Path
    description: str = ""
    persona: str = ""  # extra per-agent text appended to the master prompt
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    enabled: bool = True


@dataclass
class AppConfig:
    server: ServerConfig
    humans: list[str]
    agents: list[AgentConfig]
    source: Path | None = None

    @property
    def agent_nicks(self) -> list[str]:
        return [a.nick for a in self.agents]


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _build(cls, data: dict | None, where: str):
    data = data or {}
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {sorted(unknown)}; allowed: {sorted(known)}")
    return cls(**data)


def load_config(path: Path | str) -> AppConfig:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    raw = yaml.safe_load(path.read_text()) or {}
    return parse_config(raw, base_dir=path.parent, source=path)


def parse_config(raw: dict, base_dir: Path, source: Path | None = None) -> AppConfig:
    unknown = set(raw) - {"server", "humans", "defaults", "agents"}
    if unknown:
        raise ConfigError(f"unknown top-level key(s): {sorted(unknown)}")
    server = _build(ServerConfig, raw.get("server"), "server")
    humans = [str(h) for h in raw.get("humans") or []]
    if not humans:
        raise ConfigError("'humans' must list at least one IRC nick (you)")

    defaults = raw.get("defaults") or {}
    agents: list[AgentConfig] = []
    for i, entry in enumerate(raw.get("agents") or []):
        where = f"agents[{i}]"
        merged = _deep_merge(defaults, entry)
        nick = merged.pop("nick", None)
        if not nick:
            raise ConfigError(f"{where}: 'nick' is required")
        where = f"agent {nick!r}"
        backend = _build(BackendConfig, merged.pop("backend", None), f"{where}.backend")
        if not backend.model:
            raise ConfigError(f"{where}: backend.model is required")
        policy = _build(PolicyConfig, merged.pop("policy", None), f"{where}.policy")
        prompt = merged.pop("prompt", None)
        if not prompt:
            raise ConfigError(f"{where}: 'prompt' (path to master prompt) is required")
        prompt_path = Path(prompt).expanduser()
        if not prompt_path.is_absolute():
            prompt_path = base_dir / prompt_path
        if not prompt_path.is_file():
            raise ConfigError(f"{where}: prompt file not found: {prompt_path}")
        agent = _build(
            AgentConfig, {**merged, "nick": nick, "backend": backend, "policy": policy, "prompt": prompt_path}, where
        )
        agents.append(agent)

    agents = [a for a in agents if a.enabled]
    if not agents:
        raise ConfigError("no enabled agents configured")
    _validate_nicks([a.nick for a in agents], humans)
    return AppConfig(server=server, humans=humans, agents=agents, source=source)


def _validate_nicks(agent_nicks: list[str], humans: list[str]) -> None:
    seen: set[str] = set()
    human_set = {h.lower() for h in humans}
    for nick in agent_nicks:
        low = nick.lower()
        if not NICK_RE.match(nick):
            raise ConfigError(f"invalid IRC nick {nick!r} (letters, digits, _-[]\\`^{{|}}, max 30, not digit-first)")
        if low in RESERVED_NICKS:
            raise ConfigError(f"nick {nick!r} is reserved")
        if low in seen:
            raise ConfigError(f"duplicate agent nick {nick!r}")
        if low in human_set:
            raise ConfigError(f"agent nick {nick!r} collides with a human nick")
        seen.add(low)
