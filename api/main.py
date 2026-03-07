"""
FastAPI Backend
---------------
- WebSocket /ws  — streams all bus events to dashboard in real time
- GET /status    — agent status + system health
- GET /memory/{agent_id} — current memory state
- GET /graph     — concept graph snapshot
- GET /archives/{agent_id} — memory archives list
- POST /inject   — inject a message into the bus (from dashboard)
- POST /stop/{agent_id} / /start/{agent_id}
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

from bus.broker import bus
from api.graph import concept_graph


app = FastAPI(title="Agent Collective", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static dashboard
dashboard_dir = Path(__file__).parent.parent / "dashboard"
if dashboard_dir.exists():
    app.mount("/static", StaticFiles(directory=str(dashboard_dir)), name="static")

# Agent registry — populated by run.py
_agents: dict[str, Any] = {}
_monitor = None
_start_time = time.time()


def register_agents(agents: dict):
    global _agents
    _agents = agents


def register_monitor(monitor):
    global _monitor
    _monitor = monitor


# ── WebSocket ─────────────────────────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self.connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.connections.append(ws)

    def disconnect(self, ws: WebSocket):
        self.connections = [c for c in self.connections if c != ws]

    async def broadcast(self, data: str):
        dead = []
        for ws in self.connections:
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for d in dead:
            self.connections.remove(d)


manager = ConnectionManager()


async def bus_to_ws_loop():
    """Subscribe to bus and forward all events to WebSocket clients."""
    q = bus.subscribe()
    try:
        while True:
            event = await q.get()
            # Also update concept graph
            concept_graph.ingest(event)
            payload = json.dumps({"type": "event", "data": _serialise(event)})
            await manager.broadcast(payload)
    finally:
        bus.unsubscribe(q)


@app.on_event("startup")
async def startup():
    asyncio.create_task(bus_to_ws_loop())


@app.get("/")
async def root():
    index = dashboard_dir / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return {"status": "Agent Collective API running", "dashboard": "/index.html"}



@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    # Send recent history on connect
    history = bus.recent(n=100)
    for event in history:
        concept_graph.ingest(event)
        await ws.send_text(json.dumps({"type": "event", "data": _serialise(event)}))
    # Send initial graph
    await ws.send_text(json.dumps({"type": "graph", "data": concept_graph.to_json()}))
    try:
        while True:
            await ws.receive_text()  # keep alive
    except WebSocketDisconnect:
        manager.disconnect(ws)


# ── REST endpoints ─────────────────────────────────────────────────────────────

@app.get("/status")
async def status():
    agents_status = {}
    for agent_id, agent in _agents.items():
        agents_status[agent_id] = {
            "id":         agent.id,
            "model":      agent.model,
            "color":      agent.color,
            "running":    agent._running,
            "loop_count": agent._loop_count,
            "skills":     agent.skills.installed(),
        }
    return {
        "uptime_seconds": time.time() - _start_time,
        "agents":         agents_status,
        "bus_events":     len(bus._history),
        "concept_nodes":  len(concept_graph.nodes),
        "ws_clients":     len(manager.connections),
    }


@app.get("/memory/{agent_id}")
async def get_memory(agent_id: str):
    agent = _agents.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
    if not agent.memory:
        return {"core": "", "working": "", "status": "no_memory_engine"}
    return {
        "core":    agent.memory.read_core(),
        "working": agent.memory.read_working(),
        "status":  agent.memory.get_status(),
        "counts":  agent.memory.entry_count(),
    }


@app.get("/graph")
async def get_graph():
    return concept_graph.to_json()


@app.get("/graph/top")
async def get_top_concepts(n: int = 20):
    return concept_graph.top_concepts(n=n)


@app.get("/archives/{agent_id}")
async def get_archives(agent_id: str):
    agent = _agents.get(agent_id)
    if not agent or not agent.memory:
        raise HTTPException(status_code=404)
    return agent.memory.list_archives()


@app.get("/gpu")
async def get_gpu():
    if not _monitor:
        return {"status": "monitor_not_running"}
    return _monitor.status()


async def get_recent_events(n: int = 50, agent_id: str = None):
    return bus.recent(n=n, agent_id=agent_id)


from api.guard import check_inject

@app.post("/inject")
async def inject_message(payload: dict, request: Request):
    """Inject a message from the dashboard operator into the bus."""
    raw_msg    = payload.get("message", "")
    client_ip  = request.client.host if request.client else "unknown"

    guard = check_inject(raw_msg, client_ip)
    if not guard.ok:
        raise HTTPException(
            status_code=429 if guard.retry_after else 400,
            detail={"error": guard.reason, "retry_after": guard.retry_after},
        )

    await bus.publish({
        "agent_id":  "operator",
        "model":     "human",
        "color":     "#EF4444",
        "phase":     "system",
        "thought":   guard.sanitized,
        "concepts":  payload.get("concepts", []),
        "publish":   guard.sanitized,
        "agreements": {},
    })
    return {"ok": True}


@app.post("/stop/{agent_id}")
async def stop_agent(agent_id: str):
    agent = _agents.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404)
    await agent.stop()
    return {"ok": True, "agent_id": agent_id, "status": "stopped"}


@app.post("/start/{agent_id}")
async def start_agent(agent_id: str):
    agent = _agents.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404)
    asyncio.create_task(agent.run())
    return {"ok": True, "agent_id": agent_id, "status": "started"}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _serialise(obj):
    """Make objects JSON-safe."""
    if isinstance(obj, dict):
        return {k: _serialise(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialise(i) for i in obj]
    if isinstance(obj, set):
        return list(obj)
    return obj

# Mobile view
@app.get("/mobile")
async def mobile_view():
    mobile = dashboard_dir / "mobile.html"
    if mobile.exists():
        return FileResponse(str(mobile))
    from fastapi import HTTPException
    raise HTTPException(404, "Mobile view not found")

# SPA catch-all — MUST be last so it doesn't shadow API routes
_SPA_ROUTES = {"streams", "bus", "tools", "map", "memory", "gpu", "about"}

@app.get("/{route}")
async def spa_route(route: str):
    if route in _SPA_ROUTES:
        index = dashboard_dir / "index.html"
        if index.exists():
            return FileResponse(str(index))
    from fastapi import HTTPException
    raise HTTPException(status_code=404, detail="Not found")

# ── Session logs ──────────────────────────────────────────────────────────────
from pathlib import Path as _Path
import json as _json

_LOGS_DIR = _Path("logs")

@app.get("/logs")
async def list_sessions():
    manifest = _LOGS_DIR / "sessions.json"
    if not manifest.exists():
        return {"sessions": []}
    return _json.loads(manifest.read_text(encoding="utf-8"))

@app.get("/logs/{session_id}/summary")
async def session_summary(session_id: str):
    f = _LOGS_DIR / session_id / "summary.json"
    if not f.exists():
        from fastapi import HTTPException
        raise HTTPException(404, "Summary not found — session may still be running")
    return _json.loads(f.read_text(encoding="utf-8"))

@app.get("/logs/{session_id}/index")
async def session_index(session_id: str):
    f = _LOGS_DIR / session_id / "index.json"
    if not f.exists():
        from fastapi import HTTPException
        raise HTTPException(404, "Index not found")
    return _json.loads(f.read_text(encoding="utf-8"))

@app.get("/logs/{session_id}/chunk/{chunk_num}")
async def session_chunk(session_id: str, chunk_num: int, phase: str = None, agent_id: str = None):
    f = _LOGS_DIR / session_id / f"chunk_{chunk_num:03d}.jsonl"
    if not f.exists():
        from fastapi import HTTPException
        raise HTTPException(404, f"Chunk {chunk_num} not found")
    events = []
    for line in f.read_text(encoding="utf-8").splitlines():
        try:
            e = _json.loads(line)
            if agent_id and e.get("agent_id") != agent_id:
                continue
            if phase and e.get("phase") != phase:
                continue
            events.append(e)
        except Exception:
            continue
    return {"session_id": session_id, "chunk": chunk_num, "total": len(events), "events": events}
