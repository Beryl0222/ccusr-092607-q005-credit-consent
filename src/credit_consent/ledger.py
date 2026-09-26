"""领域账本：在哈希链日志之上维护聚合版本、业务键索引与幂等索引。

- 所有事件经契约校验后入链，`event_id` 与 `version` 由账本单调分配；
- 时钟检查点与事件一并持久化，重启回放后时钟不回退、阶段不重置；
- `exclusive()` 提供事务区间，服务在其中读状态、做判断、成批提交，
  使"同一可用余额的扣款争用"被串行化为原子决策。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator, Optional
import threading

from .clock import ControllableClock
from .contracts import validate_event
from .storage import AppendOnlyLog

CLOCK_KIND = "clock_checkpoint"


class LedgerError(Exception):
    """事件违反账本约束（契约校验失败等）。"""


class Ledger:
    def __init__(self, log: AppendOnlyLog, schema: dict[str, Any], clock: Optional[ControllableClock] = None) -> None:
        self.log = log
        self.schema = schema
        self.clock = clock or ControllableClock()
        self._lock = threading.RLock()
        self._versions: dict[str, int] = {}
        self._by_business_key: dict[str, list[dict[str, Any]]] = {}
        self._idempotency: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._replay()

    # ---- 回放 ----

    def _replay(self) -> None:
        latest_checkpoint: Optional[str] = None
        for record in self.log.records:
            body = record["body"]
            if body.get("kind") == CLOCK_KIND:
                latest_checkpoint = body["at"]
                continue
            self._index(body)
        if latest_checkpoint is not None:
            # 仅用于恢复到持久化位置；时钟不允许回退。
            self.clock.set_to(datetime.fromisoformat(latest_checkpoint))

    def _index(self, event: dict[str, Any]) -> None:
        agg = event["aggregate_id"]
        self._versions[agg] = max(self._versions.get(agg, 0), event["version"])
        payload = event.get("payload", {})
        bk = payload.get("business_key")
        if bk:
            self._by_business_key.setdefault(bk, []).append(event)
        idem = payload.get("idempotency_key")
        if bk and idem:
            self._idempotency[(bk, event["event_type"], idem)] = event

    # ---- 查询 ----

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def events_for(self, business_key: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._by_business_key.get(business_key, ()))

    def all_events(self) -> list[dict[str, Any]]:
        with self._lock:
            return [r["body"] for r in self.log.records if r["body"].get("kind") != CLOCK_KIND]

    def find_idempotent(self, business_key: str, event_type: str, idempotency_key: str) -> Optional[dict[str, Any]]:
        return self._idempotency.get((business_key, event_type, idempotency_key))

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        """串行化并发命令，保证扣款争用等决策原子完成。"""
        self._lock.acquire()
        try:
            yield
        finally:
            self._lock.release()

    # ---- 提交 ----

    def _build_event(
        self,
        seq: int,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """组装信封并校验，不触碰日志与索引。"""
        version = self._versions.get(aggregate_id, 0) + 1
        event = {
            "event_id": f"evt-{seq:08d}",
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": self.clock.now.isoformat(),
            "version": version,
            "payload": payload,
        }
        issues = validate_event(event, self.schema)
        if issues:
            detail = "; ".join(f"{i.field}:{i.code}" for i in issues)
            raise LedgerError(f"事件未通过契约校验（{event_type}）：{detail}")
        return event

    def append_event(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """组装信封、校验并追加；必须在独占事务（ledger.exclusive）内调用。"""
        event = self._build_event(
            len(self.log.records) + 1, event_type, aggregate_type, aggregate_id, payload
        )
        self.log.append(event)
        self._index(event)
        return event

    def commit_batch(self, specs: list[tuple[str, str, str, dict[str, Any]]]) -> list[dict[str, Any]]:
        """同一事务内成组提交：整批先组装并通过校验，再一次性入链，随后建索引。

        任一事件不合法则整批不落盘，日志中永远不会出现半成品批次。
        """
        with self._lock:
            staged: list[dict[str, Any]] = []
            projected_versions = dict(self._versions)
            for index, (event_type, aggregate_type, aggregate_id, payload) in enumerate(specs):
                event = self._build_event(
                    len(self.log.records) + index + 1,
                    event_type,
                    aggregate_type,
                    aggregate_id,
                    payload,
                )
                staged.append(event)
                projected_versions[aggregate_id] = event["version"]
            # 校验全部通过后才触碰仅追加日志；append_many 在同一把日志锁内落盘。
            self.log.append_many(staged)
            for event in staged:
                self._index(event)
            return staged

    def checkpoint_clock(self) -> None:
        """将当前时钟位置写入哈希链；重启据此恢复，绝不回退。"""
        with self._lock:
            self.log.append({"kind": CLOCK_KIND, "at": self.clock.now.isoformat()})
