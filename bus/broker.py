"""
Message Bus
-----------
Pure asyncio pub/sub. No external broker needed.
All agents and the API subscribe to the same event stream.
"""

import asyncio
import json
import time
import uuid
from collections import deque


class MessageBus:
    def __init__(self, history_limit: int = 200):
        self._subscribers: list[asyncio.Queue] = []
        self._history: deque = deque(maxlen=history_limit)
        self._lock = asyncio.Lock()
        self._logger = None  # set by run.py after EventLogger is created

    def set_logger(self, logger):
        self._logger = logger

    def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self._subscribers = [s for s in self._subscribers if s is not q]

    async def publish(self, event: dict):
        if "id" not in event:
            event["id"] = str(uuid.uuid4())
        if "ts" not in event:
            event["ts"] = time.time()

        async with self._lock:
            self._history.append(event)

        if self._logger:
            self._logger.write(event)

        for q in self._subscribers:
            await q.put(event)

    def recent(self, n: int = 20, agent_id: str = None) -> list[dict]:
        events = list(self._history)
        if agent_id:
            events = [e for e in events if e.get("agent_id") == agent_id]
        return events[-n:]

    def recent_published(self, n: int = 20, exclude_agent: str = None) -> list[dict]:
        events = [
            e for e in self._history
            if e.get("publish") and e.get("agent_id") != exclude_agent
        ]
        return events[-n:]

    def all_concepts(self) -> dict[str, int]:
        freq = {}
        for event in self._history:
            for concept in event.get("concepts", []):
                freq[concept] = freq.get(concept, 0) + 1
        return freq

    def to_json(self, event: dict) -> str:
        return json.dumps(event, ensure_ascii=False)


# Global singleton
bus = MessageBus()
