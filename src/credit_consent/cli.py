"""命令行入口。

用法：
  python -m credit_consent.cli <schema.json> <event.json>   # 校验单个事件（兼容旧用法）
  python -m credit_consent.cli validate <schema.json> <event.json>
  python -m credit_consent.cli verify <ledger.jsonl>        # 校验哈希链完整性
"""

import json
import sys
from pathlib import Path

from .contracts import validate_event
from .storage import AppendOnlyLog, TamperDetected


def _validate(schema_path: str, event_path: str) -> int:
    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    issues = validate_event(event, schema)
    if not issues:
        print("valid")
        return 0
    for issue in issues:
        print(f"{issue.field}	{issue.code}	{issue.message}")
    return 1


def _verify(log_path: str) -> int:
    log = AppendOnlyLog(log_path)
    try:
        tip = log.verify()
    except TamperDetected as exc:
        print(f"tampered	{exc}")
        return 1
    print(f"verified	{len(log.records)}	{tip}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 3 and args[0] == "validate":
        return _validate(args[1], args[2])
    if len(args) == 2 and args[0] == "verify":
        return _verify(args[1])
    if len(args) == 2:
        return _validate(args[0], args[1])
    print(
        "用法:\n"
        "  python -m credit_consent.cli <schema.json> <event.json>\n"
        "  python -m credit_consent.cli validate <schema.json> <event.json>\n"
        "  python -m credit_consent.cli verify <ledger.jsonl>",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
