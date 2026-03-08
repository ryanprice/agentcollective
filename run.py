#!/usr/bin/env python3
"""
Agent Collective — Main Entry Point
------------------------------------
Usage:
  python run.py                  # start everything
  python run.py               # runs with snapshot on by default
  python run.py --no-snapshot  # disable memory commit on exit
  python run.py --agents qwen,llama
  python run.py --no-api
"""

import argparse
import asyncio
import logging
import subprocess
import sys
from datetime import datetime

import yaml
import uvicorn

from agents.agent import Agent
from api.main import app, register_agents
from bus.broker import bus
from tools.gpu_monitor import GPUMonitor, MonitorConfig
from logger.event_log import EventLogger

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("collective")


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


async def main(config: dict, agent_filter=None, run_api=True, snapshot=True):
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

    # Session event logger
    event_logger = EventLogger()
    bus.set_logger(event_logger)

    # GPU monitor
    gpu_cfg = MonitorConfig.from_dict(config.get("gpu_monitor", {}))
    monitor = GPUMonitor(gpu_cfg, agents)
    from api.main import register_monitor
    register_monitor(monitor)

    await bus.publish({
        "agent_id": "system", "model": "collective", "color": "#6366f1",
        "phase": "system",
        "thought": f"Agent Collective starting. {len(agents)} agents: {', '.join(agents.keys())}",
        "concepts": [], "publish": None,
    })

    agent_tasks = [
        asyncio.create_task(agent.run(), name=f"agent-{aid}")
        for aid, agent in agents.items()
    ]
    monitor_task = asyncio.create_task(monitor.run(), name="gpu-monitor")

    if run_api:
        api_cfg = config.get("api", {})
        host = api_cfg.get("host", "0.0.0.0")
        port = api_cfg.get("port", 8000)
        log.info(f"Dashboard: http://{host if host != '0.0.0.0' else 'localhost'}:{port}")

        server_config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        server = uvicorn.Server(server_config)
        server.install_signal_handlers = lambda: None

        try:
            await asyncio.gather(*agent_tasks, monitor_task, server.serve())
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            await _shutdown(agents, agent_tasks, monitor, monitor_task, snapshot)
    else:
        try:
            await asyncio.gather(*agent_tasks, monitor_task)
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            await _shutdown(agents, agent_tasks, monitor, monitor_task, snapshot)


async def _shutdown(agents, tasks, monitor, monitor_task, snapshot=False):
    log.info("Shutting down...")
    monitor.stop()
    for agent in agents.values():
        await agent.stop()
    all_tasks = list(tasks) + [monitor_task]
    for task in all_tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*all_tasks, return_exceptions=True)

    # Close event logger — writes summary.json
    if bus._logger:
        summary = bus._logger.close(agents)
        log.info(f"Session summary: {summary['total_events']} events, "
                 f"top concepts: {[c['concept'] for c in summary['top_concepts'][:5]]}")

    # Cleanly shut down the thread pool executor
    from agents.agent import _executor
    _executor.shutdown(wait=False, cancel_futures=True)

    log.info("All agents stopped.")
    if snapshot:
        _commit_memory_snapshot()


def _commit_memory_snapshot():
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    log.info("Snapshotting agent memory to git...")
    try:
        subprocess.run(["git", "add", "memory/"], check=True)
        result = subprocess.run(["git", "diff", "--cached", "--quiet"], capture_output=True)
        if result.returncode == 1:
            subprocess.run(["git", "commit", "-m", f"snapshot: agent memory [{ts}]"], check=True)
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
    parser.add_argument("--agents",   default=None)
    parser.add_argument("--no-api",   action="store_true")
    parser.add_argument("--snapshot", action="store_true", default=True)
    parser.add_argument("--no-snapshot", action="store_false", dest="snapshot", help="Disable memory snapshot on exit")
    args = parser.parse_args()

    config       = load_config(args.config)
    agent_filter = args.agents.split(",") if args.agents else None

    try:
        asyncio.run(main(config, agent_filter=agent_filter, run_api=not args.no_api, snapshot=args.snapshot))
    except KeyboardInterrupt:
        log.info("Stopped.")
