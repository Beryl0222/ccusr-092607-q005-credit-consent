import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from credit_consent.ledger import GENESIS_HASH, Ledger, LedgerError, LedgerIntegrityError, canonical_hash, make_event

from helpers import build_service, happy_case, load_schema  # noqa: E402


def base_event(**overrides):
    event = make_event(
        event_type="INTENT_CREATED",
        aggregate_type="payment_intent",
        aggregate_id="intent-x",
        case_id="case-x",
        occurred_at="2026-09-26T10:00:00+08:00",
        version=1,
        payload={"business_key": "k1", "amount": "88.00", "currency": "CNY", "ui_hash": "ui-v1"},
        business_key="k1",
    )
    event.update(overrides)
    return event


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = load_schema()

    def test_hash_chain_links_and_verifies(self) -> None:
        ledger = Ledger(schema=self.schema)
        first = ledger.append(base_event())
        self.assertEqual(first.prev_hash, GENESIS_HASH)
        second = ledger.append(
            make_event(
                event_type="PAYMENT_CONFIRMED",
                aggregate_type="payment_intent",
                aggregate_id="intent-x",
                case_id="case-x",
                occurred_at="2026-09-26T10:01:00+08:00",
                version=2,
                payload={"action_ref": "a", "ui_hash": "ui-v1", "business_key": "k2"},
            )
        )
        self.assertEqual(second.prev_hash, first.event_hash)
        ledger.verify()

    def test_version_must_be_monotonic_per_aggregate(self) -> None:
        ledger = Ledger(schema=self.schema)
        ledger.append(base_event())
        with self.assertRaises(LedgerError):
            ledger.append(
                make_event(
                    event_type="PROMO_APPLIED",
                    aggregate_type="payment_intent",
                    aggregate_id="intent-x",
                    case_id="case-x",
                    occurred_at="2026-09-26T10:05:00+08:00",
                    version=3,
                    payload={
                        "promo_ref": "p",
                        "condition_text": "c",
                        "discount_amount": "1",
                        "ui_hash": "ui-v1",
                    },
                )
            )

    def test_business_key_is_idempotent(self) -> None:
        ledger = Ledger(schema=self.schema)
        first = ledger.append(base_event())
        # 网络重试：同键同内容直接返回首次记录，不产生第二条
        retry = ledger.append(base_event(event_id="evt-other-id"))
        self.assertIs(first, retry)
        self.assertEqual(len(ledger), 1)

    def test_tampering_with_history_is_detected_on_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            ledger = Ledger(path=path, schema=self.schema)
            ledger.append(base_event())
            ledger.append(
                make_event(
                    event_type="PAYMENT_CONFIRMED",
                    aggregate_type="payment_intent",
                    aggregate_id="intent-x",
                    case_id="case-x",
                    occurred_at="2026-09-26T10:01:00+08:00",
                    version=2,
                    payload={"action_ref": "a", "ui_hash": "ui-v1", "business_key": "k2"},
                )
            )
            # 直接改写磁盘上的历史金额
            lines = path.read_text(encoding="utf-8").splitlines()
            record = json.loads(lines[0])
            record["event"]["payload"]["amount"] = "0.01"
            lines[0] = json.dumps(record, ensure_ascii=False)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaises(LedgerIntegrityError):
                Ledger(path=path, schema=self.schema)

    def test_replay_restores_state_without_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            ledger, clock, service = build_service(path)
            happy_case(service, "case-restart")
            service.capture_credit_consent  # noqa: B018
            del ledger, service
            ledger2, clock2, service2 = build_service(path)
            state = service2._state("case-restart")
            self.assertEqual(state.phase, "captured")
            self.assertIn("credit_agreement", state.consents)
            # 同键重试在重启后依旧幂等
            again = service2.capture_credit_consent(
                "case-restart", "switch-monthly-001", "offer-v1", "ui-credit-v1", "credit:case-restart"
            )
            self.assertEqual(again.event["business_key"], "credit:case-restart")
            self.assertEqual(len(ledger2), len(Ledger(path=path, schema=self.schema)))

    def test_canonical_hash_is_stable(self) -> None:
        self.assertEqual(canonical_hash({"a": 1, "b": 2}), canonical_hash({"b": 2, "a": 1}))


if __name__ == "__main__":
    unittest.main()
