"""仅追加的哈希链事件日志。

磁盘上是 JSONL：每条记录包含序号、上一条记录的哈希、记录体与本条哈希。
记录只能追加；回放时逐行校验哈希链，任何改写、删除都会被发现。
另维护一个独立的末端指针旁车文件（<日志名>.head），记录末端序号与哈希，
因此仅删除最后一条记录这种"截断后链条仍自洽"的篡改也能被检出。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any, Iterable, Optional

GENESIS_HASH = hashlib.sha256(b"GENESIS").hexdigest()


class TamperDetected(Exception):
    """哈希链校验失败：日志被覆盖、删除或损坏。"""


def _canonical(body: dict[str, Any]) -> bytes:
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _hash_chain(prev_hash: str, body: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    digest.update(prev_hash.encode("ascii"))
    digest.update(b"\n")
    digest.update(_canonical(body))
    return digest.hexdigest()


class AppendOnlyLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.head_path = self.path.with_suffix(self.path.suffix + ".head")
        self._lock = threading.RLock()
        self._records: list[dict[str, Any]] = []
        if self.path.exists():
            self._records = self._read_all()
            self._verify_head_pointer()

    @property
    def records(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._records)

    def _read_all(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        prev_hash = GENESIS_HASH
        with self.path.open("r", encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    body = record["body"]
                    stored_hash = record["hash"]
                    stored_prev = record["prev_hash"]
                    stored_seq = record["seq"]
                except (json.JSONDecodeError, KeyError, TypeError) as exc:
                    raise TamperDetected(f"第 {lineno} 行记录损坏：{exc}") from exc
                if stored_seq != lineno:
                    raise TamperDetected(f"第 {lineno} 行序号断裂：读到 {stored_seq}")
                if stored_prev != prev_hash:
                    raise TamperDetected(f"第 {lineno} 行前向哈希不匹配，日志可能被改写")
                expected = _hash_chain(prev_hash, body)
                if stored_hash != expected:
                    raise TamperDetected(f"第 {lineno} 行内容哈希不匹配，日志可能被覆盖")
                records.append(record)
                prev_hash = stored_hash
        return records

    def _verify_head_pointer(self) -> None:
        """末端指针独立指向最后一条；缺失、滞后或不匹配都意味着删除/截断。"""
        if not self.head_path.exists():
            # 旧日志可能没有旁车；以当前末端建立，之后任何截断都可检出。
            self._write_head_pointer()
            return
        try:
            pointer = json.loads(self.head_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise TamperDetected(f"末端指针损坏：{exc}") from exc
        tip = self._records[-1] if self._records else None
        if tip is None or pointer.get("seq") != tip["seq"] or pointer.get("hash") != tip["hash"]:
            raise TamperDetected("末端指针与日志不一致：检测到记录被删除或截断")

    def _write_head_pointer(self) -> None:
        tip = self._records[-1] if self._records else {"seq": 0, "hash": GENESIS_HASH}
        pointer = {"seq": tip["seq"], "hash": tip["hash"]}
        tmp = self.head_path.with_suffix(self.head_path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(pointer, handle, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, self.head_path)

    def append(self, body: dict[str, Any]) -> dict[str, Any]:
        """追加一条记录并落盘，返回带哈希的完整记录。"""
        with self._lock:
            prev_hash = self._records[-1]["hash"] if self._records else GENESIS_HASH
            record = {
                "seq": len(self._records) + 1,
                "prev_hash": prev_hash,
                "body": body,
            }
            record["hash"] = _hash_chain(prev_hash, body)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
            self._records.append(record)
            self._write_head_pointer()
            return record

    def append_many(self, bodies: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        """在同一把锁内连续追加，保证批次对并发读取者表现为一个原子序列。"""
        written: list[dict[str, Any]] = []
        with self._lock:
            for body in bodies:
                written.append(self.append(body))
        return written

    def verify(self) -> Optional[str]:
        """完整重算哈希链并核对末端指针；完好返回末端哈希，否则抛出 TamperDetected。"""
        with self._lock:
            prev_hash = GENESIS_HASH
            for record in self._records:
                if record["prev_hash"] != prev_hash:
                    raise TamperDetected(f"第 {record['seq']} 行前向哈希不匹配")
                if record["hash"] != _hash_chain(prev_hash, record["body"]):
                    raise TamperDetected(f"第 {record['seq']} 行内容哈希不匹配")
                prev_hash = record["hash"]
            self._verify_head_pointer()
            return prev_hash
