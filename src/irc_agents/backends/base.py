"""Backend interface + registry. A backend turns a chat history into one reply.

Adding a provider (Claude, OpenAI, ...) = one module with a ``Backend`` subclass decorated with
``@register_backend("<type>")`` and imported in ``backends/__init__.py``; config then uses ``backend.type: <type>``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Literal, Sequence

from ..config import BackendConfig

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str


class BackendError(RuntimeError):
    """Raised for backend failures that should be reported, not crash the agent."""


class Backend(ABC):
    def __init__(self, config: BackendConfig) -> None:
        self.config = config

    @property
    def label(self) -> str:
        return f"{self.config.type}:{self.config.model}"

    async def check(self) -> None:
        """Preflight (reachability, model availability). Raise BackendError with a helpful message on problems."""

    @abstractmethod
    async def generate(self, messages: Sequence[ChatMessage]) -> str:
        """Return the assistant reply for ``messages`` (first message is the system prompt)."""

    async def aclose(self) -> None:
        """Release resources."""


BackendFactory = Callable[[BackendConfig], Backend]
_REGISTRY: dict[str, BackendFactory] = {}


def register_backend(type_name: str) -> Callable[[BackendFactory], BackendFactory]:
    def decorator(factory: BackendFactory) -> BackendFactory:
        _REGISTRY[type_name] = factory
        return factory

    return decorator


def available_backends() -> list[str]:
    return sorted(_REGISTRY)


def create_backend(config: BackendConfig) -> Backend:
    try:
        factory = _REGISTRY[config.type]
    except KeyError:
        raise BackendError(f"unknown backend type {config.type!r}; available: {available_backends()}") from None
    return factory(config)
