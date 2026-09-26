"""不可覆盖的穿透账账本。

- 只追加（append-only），任何修改只能通过新事件表达；
- 每条记录包含前一条记录哈希，形成哈希链，重放时逐条校验；
- business_key 提供调用方幂等：同一业务键只能成功落账一次；
- 可选 JSONL 持久化，崩溃重启后从事件流完整重建，阶段不被重置；
- 串行化临界区供上层完成"同一可用余额的原子扣款"。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .contracts import ContractIssue, validate_event


class LedgerError(RuntimeError):
    """账本层违规（版本回退、重复事件等）。"""


class LedgerIntegrityError(LedgerError):
    """哈希链或存储内容被破坏、覆盖。"""


GENESIS_HASH = "0" * 64


def canonical_hash(data: Any) -> str:
    """对任意可 JSON 化数据计算稳定 SHA-256。"""
    blob = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class StoredEvent:
    seq: int
    event_hash: str
    prev_hash: str
    event: dict[str, Any]

    def to_record(self) -> dict[str, Any]:
        return {"seq": self.seq, "prev_hash": self.prev_hash, "event_hash": self.event_hash, "event": self.event}

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "StoredEvent":
        return cls(
            seq=int(record["seq"]),
            event_hash=str(record["event_hash"]),
            prev_hash=str(record["prev_hash"]),
            event=dict(record["event"]),
        )


def _next_event_id(event_type: str, aggregate_id: str, seq: int) -> str:
    seed = canonical_hash((event_type, aggregate_id, seq))
    return f"evt-{seq:08d}-{seed[:12]}"


def make_event(
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    occurred_at: str,
    version: int,
    payload: Mapping[str, Any],
    case_id: str | None = None,
    business_key: str | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    """构造符合信封契约的事件；event_id 默认由内容派生，天然幂等。"""
    body: dict[str, Any] = {
        "event_type": event_type,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "occurred_at": occurred_at,
        "version": version,
        "payload": dict(payload),
    }
    if case_id:
        body["case_id"] = case_id
    if business_key is not None:
        body["business_key"] = business_key
    if event_id is None:
        event_id = _next_event_id(event_type, aggregate_id, version)
    return {"event_id": event_id, **body}


class Ledger:
    """内存哈希链账本，path 给定时逐条持久化到 JSONL。"""

    def __init__(self, path: str | os.PathLike[str] | None = None, schema: Mapping[str, Any] | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self.schema = schema
        self._lock = threading.RLock()
        self._records: list[StoredEvent] = []
        self._by_id: dict[str, StoredEvent] = {}
        self._by_business_key: dict[str, StoredEvent] = {}
        self._versions: dict[tuple[str, str], int] = {}
        if self.path is not None and self.path.exists():
            self._replay()

    # ---- 基础属性 -------------------------------------------------

    @property
    def lock(self) -> threading.RLock:
        """串行化所有状态变更；扣款争用同一余额时在同一临界区内完成检查与落账。"""
        return self._lock

    def __len__(self) -> int:
        return len(self._records)

    @property
    def head_hash(self) -> str:
        return self._records[-1].event_hash if self._records else GENESIS_HASH

    # ---- 追加 -----------------------------------------------------

    def append(self, event: Mapping[str, Any]) -> StoredEvent:
        """校验、查重、接哈希链并持久化；重复 business_key 返回首次记录。"""
        issues: Sequence[ContractIssue] = (
            validate_event(event, self.schema) if self.schema is not None else []
        )
        if issues:
            detail = "; ".join(f"{i.field}:{i.code}" for i in issues)
            raise LedgerError(f"事件不符合契约: {detail}")
        key = event.get("business_key")
        with self._lock:
            if isinstance(key, str) and key in self._by_business_key:
                return self._by_business_key[key]
            event_id = event["event_id"]
            if event_id in self._by_id:
                raise LedgerError(f"事件标识重复: {event_id}")
            version = int(event["version"])
            version_key = (event["aggregate_type"], event["aggregate_id"])
            last_version = self._versions.get(version_key, 0)
            if version != last_version + 1:
                raise LedgerError(
                    f"聚合版本必须连续递增: {version_key} 期望 {last_version + 1}，收到 {version}"
                )
            seq = len(self._records) + 1
            prev_hash = self.head_hash
            event_hash = canonical_hash({"prev_hash": prev_hash, "event": event})
            stored = StoredEvent(seq=seq, prev_hash=prev_hash, event_hash=event_hash, event=dict(event))
            self._persist(stored)
            self._records.append(stored)
            self._by_id[event_id] = stored
            self._versions[version_key] = version
            if isinstance(key, str):
                self._by_business_key[key] = stored
            return stored

    def get_by_business_key(self, business_key: str) -> StoredEvent | None:
        with self._lock:
            return self._by_business_key.get(business_key)

    # ---- 查询 -----------------------------------------------------

    def records(self) -> tuple[StoredEvent, ...]:
        with self._lock:
            return tuple(self._records)

    def events(self) -> tuple[dict[str, Any], ...]:
        return tuple(r.event for r in self.records())

    def for_case(self, case_id: str) -> list[dict[str, Any]]:
        return [r.event for r in self.records() if r.event.get("case_id") == case_id]

    def for_aggregate(self, aggregate_type: str, aggregate_id: str) -> list[dict[str, Any]]:
        return [
            r.event
            for r in self.records()
            if r.event["aggregate_type"] == aggregate_type and r.event["aggregate_id"] == aggregate_id
        ]

    def version_of(self, aggregate_type: str, aggregate_id: str) -> int:
        with self._lock:
            return self._versions.get((aggregate_type, aggregate_id), 0)

    # ---- 校验 -----------------------------------------------------

    def verify(self) -> None:
        """重算整条哈希链；任何覆盖、删改都会在此暴露。"""
        with self._lock:
            prev_hash = GENESIS_HASH
            seen_ids: set[str] = set()
            for index, stored in enumerate(self._records, start=1):
                if stored.seq != index:
                    raise LedgerIntegrityError(f"序号不连续: 位置 {index} 记录 seq={stored.seq}")
                if stored.prev_hash != prev_hash:
                    raise LedgerIntegrityError(f"记录 {stored.seq} 前向哈希断裂")
                rebuilt = canonical_hash({"prev_hash": prev_hash, "event": stored.event})
                if rebuilt != stored.event_hash:
                    raise LedgerIntegrityError(f"记录 {stored.seq} 内容哈希不匹配，事件被改写")
                if stored.event["event_id"] in seen_ids:
                    raise LedgerIntegrityError(f"事件标识重复: {stored.event['event_id']}")
                seen_ids.add(stored.event["event_id"])
                prev_hash = stored.event_hash

    # ---- 内部 -----------------------------------------------------

    def _persist(self, stored: StoredEvent) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(stored.to_record(), ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _replay(self) -> None:
        assert self.path is not None
        prev_hash = GENESIS_HASH
        for line_no, raw in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
                stored = StoredEvent.from_record(record)
            except (ValueError, KeyError, TypeError) as exc:
                raise LedgerIntegrityError(f"第 {line_no} 行记录无法解析: {exc}") from exc
            if stored.seq != len(self._records) + 1:
                raise LedgerIntegrityError(f"第 {line_no} 行序号不连续")
            if stored.prev_hash != prev_hash:
                raise LedgerIntegrityError(f"第 {line_no} 行前向哈希断裂")
            rebuilt = canonical_hash({"prev_hash": prev_hash, "event": stored.event})
            if rebuilt != stored.event_hash:
                raise LedgerIntegrityError(f"第 {line_no} 行内容哈希不匹配，历史被覆盖")
            key = stored.event.get("business_key")
            if isinstance(key, str):
                if key in self._by_business_key:
                    raise LedgerIntegrityError(f"第 {line_no} 行业务键重复: {key}")
                self._by_business_key[key] = stored
            version_key = (stored.event["aggregate_type"], stored.event["aggregate_id"])
            version = int(stored.event["version"])
            if version != self._versions.get(version_key, 0) + 1:
                raise LedgerIntegrityError(f"第 {line_no} 行聚合版本不连续: {version_key}")
            self._versions[version_key] = version
            self._by_id[stored.event["event_id"]] = stored
            self._records.append(stored)
            prev_hash = stored.event_hash

    @contextlib.contextmanager
    def exclusive(self) -> Iterator[None]:
        """显式临界区：跨多事件的读-判断-写必须原子完成。"""
        with self._lock:
            yield
