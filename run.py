#!/usr/bin/env python3
"""
Agent Collective — Main Entry Point
------------------------------------
Usage:
  python run.py                  # start everything
  python run.py --config path/to/config.yaml
  python run.py --agents qwen,llama   # start subset
  python run.py --no-api              # agents only (no dashboard)
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import yaml
import uvicorn

from agents.agent import Agent
from api.main import app, register_agents
from bus.broker import bus


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("collective")


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


async def main(config: dict, agent_filter: list[str] = None, run_api: bool = True):
    agent_configs = config.get("agents", [])
    if agent_filter:
        agent_configs = [a for a in agent_configs if a["id"] in agent_filter]

    if not agent_configs:
        log.error("No agents configured or matched filter.")
        sys.exit(1)

    log.info(f"Starting {len(agent_configs)} agents...")

    # Instantiate agents
    agents = {}
    for ac in agent_configs:
        agent = Agent(ac, config)
        agents[ac["id"]] = agent
        log.info(f"  ✓ {ac['id']} ({ac['model']})")

    register_agents(agents)

    # Publish startup event
    await bus.publish({
        "agent_id": "system",
        "model":    "collective",
        "color":    "#6366f1",
        "phase":    "system",
        "thought":  f"Agent Collective starting. {len(agents)} agents: {', '.join(agents.keys())}",
        "concepts": [],
        "publish":  None,
    })

    # Start agent loops
    agent_tasks = [
        asyncio.create_task(agent.run(), name=f"agent-{agent_id}")
        for agent_id, agent in agents.items()
    ]

    if run_api:
        api_config = config.get("api", {})
        host = api_config.get("host", "0.0.0.0")
        port = api_config.get("port", 8000)

        log.info(f"Dashboard: http://{host if host != '0.0.0.0' else 'localhost'}:{port}")

        server_config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="warning",
        )
        server = uvicorn.Server(server_config)
        # Let uvicorn handle its own signal handling
        server.install_signal_handlers = lambda: None

        try:
            await asyncio.gather(*agent_tasks, server.serve())
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            await _shutdown(agents, agent_tasks)
    else:
        try:
            await asyncio.gather(*agent_tasks)
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            await _shutdown(agents, agent_tasks)


async def _shutdown(agents: dict, tasks: list):
    log.info("Shutting down...")
    for agent in agents.values():
        await agent.stop()
    for task in tasks:
        if not task.done():
            task.cancel()
    # Wait briefly for cancellations to propagate
    await asyncio.gather(*tasks, return_exceptions=True)
    log.info("All agents stopped.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Agent Collective")
    parser.add_argument("--config",  default="config.yaml")
    parser.add_argument("--agents",  default=None, help="Comma-separated agent IDs to start")
    parser.add_argument("--no-api",  action="store_true", help="Skip API/dashboard server")
    args = parser.parse_args()

    config       = load_config(args.config)
    agent_filter = args.agents.split(",") if args.agents else None

    try:
        asyncio.run(main(config, agent_filter=agent_filter, run_api=not args.no_api))
    except KeyboardInterrupt:
        log.info("Stopped.")
