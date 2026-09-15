"""Command line: ``python -m teai_agent`` runs the agent until SIGTERM/SIGINT."""

import asyncio
import logging
import signal
import sys

from . import __version__
from .agent import Agent
from .config import AgentConfig, ConfigError


async def _main(config):
    agent = Agent(config)
    loop = asyncio.get_running_loop()
    # SIGINT (Ctrl-C) always stops at once; SIGTERM ("docker compose stop")
    # drains first when AGENT_DRAIN_ON_TERM is on, so in-flight requests finish.
    handlers = {signal.SIGINT: agent.request_stop, signal.SIGTERM: agent.handle_term}
    for sig, handler in handlers.items():
        try:
            loop.add_signal_handler(sig, handler)
        except (NotImplementedError, RuntimeError):  # Windows / non-main thread
            pass
    await agent.run()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help", "help"):
        print("Usage: python -m teai_agent   (configured via environment, see .env.example)")
        return 0
    try:
        config = AgentConfig.from_environ()
    except ConfigError as exc:
        print("Configuration error: {0}".format(exc), file=sys.stderr)
        return 2
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("teai_agent").info(
        "TextEnhanceAI GPU agent %s starting as %s (relay %s, vLLM %s, concurrency %d)",
        __version__, config.agent_name, config.relay_ws_url, config.vllm_base_url, config.max_concurrency,
    )
    try:
        asyncio.run(_main(config))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
