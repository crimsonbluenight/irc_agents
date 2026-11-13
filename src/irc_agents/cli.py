"""Command line entry point: ``uv run irc-agents --config agents.yaml``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from .agent import Agent, render_prompt
from .backends import BackendError, create_backend
from .config import AppConfig, ConfigError, load_config

log = logging.getLogger("irc_agents")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="irc-agents", description="Run LLM agents on an IRC channel.")
    parser.add_argument("-c", "--config", type=Path, default=Path("agents.yaml"), help="YAML config (agents.yaml)")
    parser.add_argument("--only", action="append", metavar="NICK", help="start only this agent (repeatable)")
    parser.add_argument("--check", action="store_true", help="validate config + backends, then exit")
    parser.add_argument("--show-prompt", metavar="NICK", help="print the rendered master prompt of an agent and exit")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser


async def preflight(app: AppConfig, nicks: list[str]) -> bool:
    ok = True
    for agent_cfg in app.agents:
        if agent_cfg.nick not in nicks:
            continue
        backend = create_backend(agent_cfg.backend)
        try:
            await backend.check()
            log.info("ok: %s -> %s", agent_cfg.nick, backend.label)
        except BackendError as exc:
            log.error("%s: %s", agent_cfg.nick, exc)
            ok = False
        finally:
            await backend.aclose()
    return ok


async def run_agents(app: AppConfig, nicks: list[str]) -> None:
    agents = [Agent(cfg, app, create_backend(cfg.backend)) for cfg in app.agents if cfg.nick in nicks]
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    tasks = []
    for agent in agents:
        tasks.append(asyncio.create_task(agent.run(), name=agent.nick))
        await asyncio.sleep(0.3)  # stagger connects
    log.info(
        "started %d agent(s) on %s:%s %s: %s",
        len(agents),
        app.server.host,
        app.server.port,
        app.server.channel,
        ", ".join(a.nick for a in agents),
    )
    await stop.wait()
    log.info("shutting down")
    await asyncio.gather(*(a.stop() for a in agents), return_exceptions=True)
    await asyncio.sleep(0.5)  # let QUITs flush
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    try:
        app = load_config(args.config)
    except ConfigError as exc:
        log.error("config error: %s", exc)
        return 2

    if args.show_prompt:
        cfg = next((a for a in app.agents if a.nick.lower() == args.show_prompt.lower()), None)
        if cfg is None:
            log.error("no agent %r; configured: %s", args.show_prompt, app.agent_nicks)
            return 2
        print(render_prompt(cfg.prompt.read_text(), cfg, app))
        return 0

    nicks = app.agent_nicks
    if args.only:
        wanted = {n.lower() for n in args.only}
        nicks = [n for n in nicks if n.lower() in wanted]
        missing = wanted - {n.lower() for n in nicks}
        if missing:
            log.error("unknown agent(s) %s; configured: %s", sorted(missing), app.agent_nicks)
            return 2

    try:
        if not asyncio.run(preflight(app, nicks)):
            return 1
        if args.check:
            log.info("config and backends OK")
            return 0
        asyncio.run(run_agents(app, nicks))
    except BackendError as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
