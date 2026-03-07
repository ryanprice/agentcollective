#!/usr/bin/env python3
"""
Agent Collective — Main Entry Point
------------------------------------
Usage:
  python run.py                  # start everything
  python run.py --config path/to/config.yaml
  python run.py --agents qwen,llama   # start subset
  python run.py --no-api              # agents only (no dashboard)
  python run.py --snapshot            # auto-commit memory to git on Ctrl+C
"""

import argparse
import asyncio
import logging
import subprocess
import sys
from datetime import datetime
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


async def main(config: dict, agent_filter: list[str] = None, run_api: bool = True, snapshot: bool = False):
    agent_configs = config.get("agents", [])
    if agent_filter:
        agent_configs = [a for a in agent_configs if a["id"] in agent_filter]

    if not agent_configs:
        log.error("No agents configured or matched filter.")
        sys.exit(1)

    log.info(f"Starting {len(agent_configs)} agents...")

    agents = {}
    for ac in agent_configs:
        agent = Agent(ac, config)
        agents[ac["id"]] = agent
        log.info(f"  ✓ {ac['id']} ({ac['model']})")

    register_agents(agents)

    await bus.publish({
        "agent_id": "system",
        "model":    "collective",
        "color":    "#6366f1",
        "phase":    "system",
        "thought":  f"Agent Collective starting. {len(agents)} agents: {', '.join(agents.keys())}",
        "concepts": [],
        "publish":  None,
    })

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
        server.install_signal_handlers = lambda: None

        try:
            await asyncio.gather(*agent_tasks, server.serve())
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            await _shutdown(agents, agent_tasks, snapshot=snapshot)
    else:
        try:
            await asyncio.gather(*agent_tasks)
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            await _shutdown(agents, agent_tasks, snapshot=snapshot)


async def _shutdown(agents: dict, tasks: list, snapshot: bool = False):
    log.info("Shutting down...")
    for agent in agents.values():
        await agent.stop()
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    log.info("All agents stopped.")

    if snapshot:
        _commit_memory_snapshot()


def _commit_memory_snapshot():
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    log.info("Snapshotting agent memory to git...")
    try:
        subprocess.run(["git", "add", "memory/"], check=True)
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            capture_output=True
        )
        if result.returncode == 1:  # staged changes exist
            subprocess.run(
                ["git", "commit", "-m", f"snapshot: agent memory [{ts}]"],
                check=True,
            )
            log.info(f"✓ Memory snapshot committed: {ts}")
        else:
            log.info("No memory changes since last snapshot.")
    except subprocess.CalledProcessError as e:
        log.warning(f"Snapshot failed: {e}")
    except FileNotFoundError:
        log.warning("git not found — skipping snapshot.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Agent Collective")
    parser.add_argument("--config",   default="config.yaml")
    parser.add_argument("--agents",   default=None, help="Comma-separated agent IDs to start")
    parser.add_argument("--no-api",   action="store_true", help="Skip API/dashboard server")
    parser.add_argument("--snapshot", action="store_true", help="Auto-commit agent memory to git on shutdown")
    args = parser.parse_args()

    config       = load_config(args.config)
    agent_filter = args.agents.split(",") if args.agents else None

    try:
        asyncio.run(main(
            config,
            agent_filter=agent_filter,
            run_api=not args.no_api,
            snapshot=args.snapshot,
        ))
    except KeyboardInterrupt:
        log.info("Stopped.")
