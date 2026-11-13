"""Local Ollama backend.

GPU memory: Ollama's scheduler decides placement. If a model does not fit next to the ones already loaded it unloads
idle models (swap from RAM page cache takes ~1-2 s) and if a single model does not fit it splits layers between GPU
and CPU. To avoid two big loads at the same time, requests to one Ollama endpoint are serialised
(``max_concurrent``, default 1). ``options.num_gpu`` pins how many layers go to the GPU if you prefer a fixed
GPU/CPU split that keeps several models resident.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Sequence

import httpx
import ollama

from .base import Backend, BackendError, ChatMessage, register_backend

log = logging.getLogger(__name__)

DEFAULT_HOST = "http://localhost:11434"
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
# One semaphore per (event loop, endpoint): all agents talking to the same Ollama share it.
_SEMAPHORES: dict[tuple[int, str], asyncio.Semaphore] = {}


def _semaphore(host: str, limit: int) -> asyncio.Semaphore:
    key = (id(asyncio.get_running_loop()), host)
    if key not in _SEMAPHORES:
        _SEMAPHORES[key] = asyncio.Semaphore(max(1, limit))
    return _SEMAPHORES[key]


@register_backend("ollama")
class OllamaBackend(Backend):
    def __init__(self, config) -> None:
        super().__init__(config)
        self.host = config.host or DEFAULT_HOST
        self.client = ollama.AsyncClient(host=self.host, timeout=config.timeout)

    async def check(self) -> None:
        try:
            listing = await self.client.list()
        except (httpx.HTTPError, ConnectionError, OSError) as exc:
            raise BackendError(f"Ollama not reachable at {self.host}: {exc}") from exc
        names = {m.model for m in listing.models}
        model = self.config.model
        if model not in names and f"{model}:latest" not in names:
            raise BackendError(f"model {model!r} is not pulled in Ollama at {self.host}; run: ollama pull {model}")

    async def generate(self, messages: Sequence[ChatMessage]) -> str:
        payload = [{"role": m.role, "content": m.content} for m in messages]
        async with _semaphore(self.host, self.config.max_concurrent):
            try:
                response = await self.client.chat(
                    model=self.config.model,
                    messages=payload,
                    options=self.config.options or None,
                    think=self.config.think,
                    keep_alive=self.config.keep_alive,
                )
            except ollama.ResponseError as exc:
                raise BackendError(f"Ollama error ({self.config.model}): {exc.error}") from exc
            except (httpx.HTTPError, ConnectionError, OSError) as exc:
                raise BackendError(f"Ollama request failed ({self.config.model}): {exc}") from exc
        content = response.message.content or ""
        return _THINK_RE.sub("", content).strip()
