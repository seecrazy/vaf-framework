"""状态持久化（DESIGN §7.4）：StateStore 协议 + 内存 / SQLite 实现。"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import List, Optional

from ..errors import VafError
from .state import PipelineState, RunStatus


class StateStore:
    def save(self, state: PipelineState) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def get(self, request_id: str) -> Optional[PipelineState]:  # pragma: no cover
        raise NotImplementedError

    def list(self, status: Optional[RunStatus] = None, pipeline: Optional[str] = None) -> List[PipelineState]:  # pragma: no cover
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover
        pass


class InMemoryStateStore(StateStore):
    def __init__(self) -> None:
        self._states: dict = {}
        self._lock = threading.Lock()

    def save(self, state: PipelineState) -> None:
        with self._lock:
            self._states[state.request_id] = state.model_copy(deep=True)

    def get(self, request_id: str) -> Optional[PipelineState]:
        with self._lock:
            state = self._states.get(request_id)
            return state.model_copy(deep=True) if state else None

    def list(self, status: Optional[RunStatus] = None, pipeline: Optional[str] = None) -> List[PipelineState]:
        with self._lock:
            states = list(self._states.values())
        if status is not None:
            states = [s for s in states if s.status == status]
        if pipeline is not None:
            states = [s for s in states if s.pipeline == pipeline]
        return sorted(states, key=lambda s: s.created_at)


class SqliteStateStore(StateStore):
    def __init__(self, path: str):
        self._path = path
        dirname = os.path.dirname(path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                request_id TEXT PRIMARY KEY,
                pipeline TEXT NOT NULL,
                domain TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                state_json TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status)")
        self._conn.commit()

    def save(self, state: PipelineState) -> None:
        payload = state.model_dump_json()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO runs (request_id, pipeline, domain, status, state_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET
                    status=excluded.status,
                    state_json=excluded.state_json,
                    updated_at=excluded.updated_at
                """,
                (
                    state.request_id,
                    state.pipeline,
                    state.domain,
                    state.status.value,
                    payload,
                    state.updated_at,
                ),
            )
            self._conn.commit()

    def get(self, request_id: str) -> Optional[PipelineState]:
        with self._lock:
            row = self._conn.execute(
                "SELECT state_json FROM runs WHERE request_id = ?", (request_id,)
            ).fetchone()
        if row is None:
            return None
        return PipelineState.model_validate_json(row[0])

    def list(self, status: Optional[RunStatus] = None, pipeline: Optional[str] = None) -> List[PipelineState]:
        query = "SELECT state_json FROM runs"
        conds, params = [], []
        if status is not None:
            conds.append("status = ?")
            params.append(status.value)
        if pipeline is not None:
            conds.append("pipeline = ?")
            params.append(pipeline)
        if conds:
            query += " WHERE " + " AND ".join(conds)
        query += " ORDER BY updated_at"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        try:
            return [PipelineState.model_validate_json(r[0]) for r in rows]
        except json.JSONDecodeError as exc:  # pragma: no cover
            raise VafError(f"corrupted state row: {exc}") from exc

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class RedisStateStore(StateStore):
    """Redis 状态存储（V2.1 P2-3）。

    client 可注入（任何实现 get/set/scan_iter/delete 的客户端，便于测试）；
    from_url() 走真实 redis-py。可选 TTL 自动清理已完成 run。
    """

    def __init__(self, client, prefix: str = "vaf:runs:", ttl_seconds: Optional[int] = None):
        # client: redis.Redis 或等价对象（get/set/delete/scan_iter）
        self._client = client
        self._prefix = prefix
        self._ttl = ttl_seconds
        self._lock = threading.Lock()

    @classmethod
    def from_url(cls, url: str, **kw) -> "RedisStateStore":
        import redis  # 延迟导入：仅使用 Redis 存储时才需要

        return cls(redis.Redis.from_url(url, decode_responses=True), **kw)

    def _key(self, request_id: str) -> str:
        return f"{self._prefix}{request_id}"

    def save(self, state: PipelineState) -> None:
        payload = state.model_dump_json()
        with self._lock:
            self._client.set(self._key(state.request_id), payload)
            if self._ttl:
                try:
                    self._client.expire(self._key(state.request_id), self._ttl)
                except Exception:  # noqa: BLE001 - TTL 失败不影响正确性
                    pass

    def get(self, request_id: str) -> Optional[PipelineState]:
        raw = self._client.get(self._key(request_id))
        if raw is None:
            return None
        return PipelineState.model_validate_json(raw)

    def list(self, status: Optional[RunStatus] = None, pipeline: Optional[str] = None) -> List[PipelineState]:
        states: List[PipelineState] = []
        for key in self._client.scan_iter(match=f"{self._prefix}*"):
            raw = self._client.get(key)
            if not raw:
                continue
            state = PipelineState.model_validate_json(raw)
            if status is not None and state.status != status:
                continue
            if pipeline is not None and state.pipeline != pipeline:
                continue
            states.append(state)
        return sorted(states, key=lambda s: s.updated_at)

    def close(self) -> None:  # pragma: no cover - 连接归属调用方
        pass
