import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from credit_consent.domain import (  # noqa: E402
    ConsentError,
    DomainError,
    FrozenCaseError,
    RuleGovernanceError,
    project_case,
)
from credit_consent.readmodel import audit_timeline, consumer_explanation, lender_view  # noqa: E402

from helpers import build_service, happy_case, publish_default_rule  # noqa: E402


class TwoDecisionTests(unittest.TestCase):
    def test_credit_consent_requires_prior_payment_confirmation(self) -> None:
        _, _, service = build_service()
        publish_default_rule(service)
        service.create_intent("c1", "88.00", "CNY", "ui-v1", "intent:c1")
        service.present_offer(
            "c1", "o1", "ui-c", "83", "90", "0.18", [], [], "lender-A",
            {"apr": "0.18"},
        )
        service.assess_suitability("c1", "o1", {"monthly_income": "10000", "monthly_payment": "100"})
        with self.assertRaises(ConsentError):
            service.capture_credit_consent("c1", "sw", "o1", "ui-c", "credit:c1")

    def test_promo_does_not_grant_consent(self) -> None:
        _, _, service = build_service()
        happy_case(service, "c2", credit=False)
        state = service._state("c2")
        self.assertNotIn("credit_agreement", state.consents)
        self.assertIsNotNone(state.promo)

    def test_credit_consent_must_be_explicit(self) -> None:
        _, _, service = build_service()
        happy_case(service, "c3", credit=False)
        with self.assertRaises(ConsentError):
            service.capture_credit_consent(
                "c3", "sw", "offer-v1", "ui-credit-v1", "credit:c3", explicit=False
            )

    def test_credit_consent_requires_suitability_eligible(self) -> None:
        _, _, service = build_service()
        happy_case(service, "c4", credit=False)
        # 高负债比 → ineligible
        service.assess_suitability(
            "c4", "offer-v1", {"monthly_income": "1000", "monthly_payment": "900"}
        )
        with self.assertRaises(ConsentError):
            service.capture_credit_consent("c4", "sw", "offer-v1", "ui-credit-v1", "credit:c4")

    def test_two_consents_are_distinct_records_with_distinct_actions(self) -> None:
        ledger, _, service = build_service()
        happy_case(service, "c5")
        state = service._state("c5")
        payment = state.consents["payment_confirmation"]
        credit = state.consents["credit_agreement"]
        self.assertNotEqual(payment.action_ref, credit.action_ref)
        self.assertEqual(payment.terms_hash, None)
        self.assertIsNotNone(credit.terms_hash)
        # 恰好两条 CONSENT_CAPTURED
        captured = [e for e in ledger.events() if e["event_type"] == "CONSENT_CAPTURED"]
        self.assertEqual(len(captured), 2)


class GovernanceTests(unittest.TestCase):
    def test_marketing_cannot_approve_rules(self) -> None:
        _, _, service = build_service()
        with self.assertRaises(RuleGovernanceError):
            service.publish_suitability_rule("r", "v1", "marketing_configurator", {"k": 1})

    def test_assessment_without_published_rule_fails(self) -> None:
        _, _, service = build_service()
        service.create_intent("g1", "88", "CNY", "ui-v1", "i:g1")
        service.present_offer("g1", "o1", "ui-c", "83", "90", "0.18", [], [], "lender-A", {"x": 1})
        with self.assertRaises(RuleGovernanceError):
            service.assess_suitability("g1", "o1", {"monthly_income": "100", "monthly_payment": "1"})


class ReconfirmationTests(unittest.TestCase):
    def _signed(self, case_id="r1"):
        _, _, service = build_service()
        happy_case(service, case_id)
        service.capture_credit_consent  # noqa
        service.sign_contract(case_id, f"contract-{case_id}", [
            {"role": "payment_platform", "ref": "payco"},
            {"role": "loan_facilitator", "ref": "broker"},
            {"role": "lender", "ref": "lender-A"},
        ], f"sign:{case_id}")
        return service

    def test_rate_change_invalidates_old_consent(self) -> None:
        service = self._signed()
        service.require_reconfirmation(
            "r1", "rate_changed", ["apr"], {"apr": "0.24", "parties": ["lender-A"]}
        )
        state = service._state("r1")
        self.assertFalse(state.offer_version_alive("offer-v1"))

    def test_disburse_blocked_until_reconfirmed_after_rate_change(self) -> None:
        service = self._signed("r2")
        service.require_reconfirmation("r2", "rate_changed", ["apr"], {"apr": "0.24"})
        # 展示新要约、重新评估、重新同意、合同不能重复签 —— 已签合同的费率变更场景：
        # 旧同意已失效，直接用旧业务键重试放款以外的关键校验：重新签约前必须先有新同意
        service.present_offer(
            "r2", "offer-v2", "ui-credit-v2", "83", "96", "0.24", [],
            [{"due": "2026-10-26", "amount": "96.00"}], "lender-A", {"apr": "0.24"}
        )
        service.assess_suitability(
            "r2", "offer-v2", {"monthly_income": "10000", "monthly_payment": "1000"}
        )
        # 未对新要约重新同意前，不能放款
        with self.assertRaises(DomainError):
            service.disburse("r2", "83.00", "lender-A", "disburse:r2")
        service.capture_credit_consent("r2", "switch-002", "offer-v2", "ui-credit-v2", "credit:r2:v2")
        # 重新确认后放款恢复
        disbursed = service.disburse("r2", "83.00", "lender-A", "disburse:r2")
        self.assertEqual(disbursed.event["event_type"], "FUNDS_DISBURSED")
        # 合同不重复签
        with self.assertRaises(DomainError):
            service.sign_contract("r2", "contract-r2", [], "sign:r2:other")


class RevocationTests(unittest.TestCase):
    def test_revoke_unused_credit_releases_limit_but_keeps_evidence(self) -> None:
        ledger, _, service = build_service()
        keys = happy_case(service, "v1")
        service.sign_contract("v1", "contract-v1", [{"role": "lender", "ref": "lender-A"}], "sign:v1")
        service.revoke_credit("v1")
        state = service._state("v1")
        self.assertEqual(state.phase, "revoked")
        self.assertEqual(state.revoked["released_amount"], "83.00")
        # 历史证据仍在
        self.assertIn("credit_agreement", state.consents)
        self.assertIsNotNone(state.contract)
        self.assertTrue(any(e["event_type"] == "OFFER_PRESENTED" for e in ledger.for_case("v1")))
        # 撤销后不得放款
        with self.assertRaises(DomainError):
            service.disburse("v1", "83.00", "lender-A", "dis:v1")

    def test_cannot_revoke_after_disbursement(self) -> None:
        _, _, service = build_service()
        happy_case(service, "v2")
        service.sign_contract("v2", "contract-v2", [{"role": "lender", "ref": "lender-A"}], "sign:v2")
        service.disburse("v2", "83.00", "lender-A", "dis:v2")
        with self.assertRaises(DomainError):
            service.revoke_credit("v2")


class IdempotencyAndFreezeTests(unittest.TestCase):
    def test_retry_with_same_key_does_not_duplicate_disbursement(self) -> None:
        ledger, _, service = build_service()
        happy_case(service, "i1")
        service.sign_contract("i1", "contract-i1", [{"role": "lender", "ref": "lender-A"}], "sign:i1")
        first = service.disburse("i1", "83.00", "lender-A", "dis:i1")
        retry = service.disburse("i1", "83.00", "lender-A", "dis:i1")
        self.assertIs(first, retry)
        disbursed = [e for e in ledger.events() if e["event_type"] == "FUNDS_DISBURSED"]
        self.assertEqual(len(disbursed), 1)

    def test_same_key_different_amount_freezes_case(self) -> None:
        _, _, service = build_service()
        happy_case(service, "i2")
        service.sign_contract("i2", "contract-i2", [{"role": "lender", "ref": "lender-A"}], "sign:i2")
        service.disburse("i2", "83.00", "lender-A", "dis:i2")
        with self.assertRaises(FrozenCaseError):
            service.disburse("i2", "999.00", "lender-A", "dis:i2")
        state = service._state("i2")
        self.assertTrue(state.is_frozen)
        self.assertEqual(state.phase, "frozen")
        self.assertIn("amount_conflict", state.frozen_reasons)
        # 冻结后一切资金动作停止
        with self.assertRaises(FrozenCaseError):
            service.charge_fee("i2", "service_fee", "7", "lender-A", "fee:i2")

    def test_same_key_different_ui_fingerprint_freezes_case(self) -> None:
        _, _, service = build_service()
        publish_default_rule(service)
        service.create_intent("i3", "88", "CNY", "ui-v1", "intent:i3")
        service.confirm_payment("i3", "btn", "ui-v1", "pay:i3")
        with self.assertRaises(FrozenCaseError):
            service.confirm_payment("i3", "btn", "ui-DIFFERENT", "pay:i3")
        self.assertTrue(service._state("i3").is_frozen)

    def test_consent_ui_must_match_presented_offer_ui(self) -> None:
        _, _, service = build_service()
        happy_case(service, "i4", credit=False)
        with self.assertRaises(FrozenCaseError):
            service.capture_credit_consent(
                "i4", "sw", "offer-v1", "ui-TAMPERED", "credit:i4"
            )


class AtomicDeductionTests(unittest.TestCase):
    def test_concurrent_deductions_compete_atomically(self) -> None:
        ledger, _, service = build_service()
        service.register_repayment_account("acct-1", "100.00", "CNY")
        happy_case(service, "d1")
        service.sign_contract("d1", "contract-d1", [{"role": "lender", "ref": "lender-A"}], "sign:d1")
        service.disburse("d1", "83.00", "lender-A", "dis:d1")
        service.issue_bill("d1", "bill-d1", "60.00", "2026-10-26T00:00:00+08:00")

        def deduct(key: str):
            return service.request_deduction("d1", f"req-{key}", "60.00", "acct-1", key)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(deduct, [f"ded:d1:{i}" for i in range(8)]))
        outcomes = sorted(r.event["payload"]["outcome"] for r in results)
        self.assertEqual(outcomes.count("settled"), 1)
        self.assertEqual(outcomes.count("insufficient_funds"), 7)
        self.assertEqual(service.available_balance("acct-1"), Decimal("40.00"))

    def test_retry_same_deduction_key_is_idempotent(self) -> None:
        _, _, service = build_service()
        service.register_repayment_account("acct-2", "100.00", "CNY")
        happy_case(service, "d2")
        service.sign_contract("d2", "contract-d2", [{"role": "lender", "ref": "lender-A"}], "sign:d2")
        service.disburse("d2", "83.00", "lender-A", "dis:d2")
        first = service.request_deduction("d2", "req-1", "30", "acct-2", "ded:d2")
        retry = service.request_deduction("d2", "req-1", "30", "acct-2", "ded:d2")
        self.assertIs(first, retry)
        self.assertEqual(service.available_balance("acct-2"), Decimal("70.00"))

    def test_duplicate_signing_is_idempotent_then_rejected(self) -> None:
        _, _, service = build_service()
        happy_case(service, "d3")
        parties = [{"role": "lender", "ref": "lender-A"}]
        first = service.sign_contract("d3", "contract-d3", parties, "sign:d3")
        retry = service.sign_contract("d3", "contract-d3", parties, "sign:d3")
        self.assertIs(first, retry)
        with self.assertRaises(DomainError):
            service.sign_contract("d3", "contract-d3", parties, "sign:d3:other")


class ClockTests(unittest.TestCase):
    def test_cooling_off_bill_overdue_collection_progress(self) -> None:
        _, clock, service = build_service()
        happy_case(service, "t1")
        service.sign_contract("t1", "contract-t1", [{"role": "lender", "ref": "lender-A"}], "sign:t1")
        self.assertEqual(service._state("t1").phase, "cooling_off")
        # 犹豫期内推进不足 7 天不生效
        clock.advance(timedelta(days=6))
        self.assertEqual(service.evaluate_time_progression("t1"), [])
        self.assertEqual(service._state("t1").phase, "cooling_off")
        clock.advance(timedelta(days=2))
        service.evaluate_time_progression("t1")
        self.assertEqual(service._state("t1").phase, "active")
        service.disburse("t1", "83.00", "lender-A", "dis:t1")
        service.issue_bill("t1", "bill-t1", "90.00", (clock.now() + timedelta(days=5)).date().isoformat() + "T00:00:00+08:00")
        clock.advance(timedelta(days=6))
        service.evaluate_time_progression("t1")
        self.assertEqual(service._state("t1").phase, "overdue")
        kinds = {n["notice_kind"] for n in service._state("t1").notices}
        self.assertIn("overdue", kinds)
        # 30 天异议/催收窗口后发催收通知，且不重复发
        clock.advance(timedelta(days=31))
        service.evaluate_time_progression("t1")
        service.evaluate_time_progression("t1")
        notices = [n for n in service._state("t1").notices if n["notice_kind"] == "collection"]
        self.assertEqual(len(notices), 1)

    def test_dispute_window_enforced(self) -> None:
        _, clock, service = build_service()
        happy_case(service, "t2")
        service.sign_contract("t2", "contract-t2", [{"role": "lender", "ref": "lender-A"}], "sign:t2")
        service.disburse("t2", "83.00", "lender-A", "dis:t2")
        due = (clock.now() + timedelta(days=1)).date().isoformat() + "T00:00:00+08:00"
        service.issue_bill("t2", "bill-t2", "90", due)
        clock.advance(timedelta(days=32))
        with self.assertRaises(DomainError):
            service.open_dispute("t2", "未看到月付提示", "user")
        clock.set(clock.now() - timedelta(days=10))
        service.open_dispute("t2", "未看到月付提示", "user")
        self.assertEqual(service._state("t2").phase, "dispute")
        service.resolve_dispute("t2", "减免7元服务费", "arbiter")
        # 解决时已过出账日，阶段回到逾期而非账单
        self.assertEqual(service._state("t2").phase, "overdue")

    def test_restart_does_not_reset_phase(self) -> None:
        import tempfile
        from pathlib import Path
        from credit_consent.ledger import Ledger
        from credit_consent.clock import ControlledClock
        from credit_consent.domain import PaymentCreditService
        import json

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "l.jsonl"
            schema = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))
            ledger = Ledger(path=path, schema=schema)
            clock = ControlledClock()
            service = PaymentCreditService(ledger, clock)
            happy_case(service, "t3")
            service.sign_contract("t3", "contract-t3", [{"role": "lender", "ref": "lender-A"}], "sign:t3")
            clock.advance(timedelta(days=8))
            service.evaluate_time_progression("t3")
            del service, ledger
            ledger2 = Ledger(path=path, schema=schema)
            service2 = PaymentCreditService(ledger2, ControlledClock(clock.now()))
            self.assertEqual(service2._state("t3").phase, "active")


class ReadModelTests(unittest.TestCase):
    def _populated(self):
        _, clock, service = build_service()
        happy_case(service, "m1")
        service.sign_contract("m1", "contract-m1", [
            {"role": "payment_platform", "ref": "payco"},
            {"role": "loan_facilitator", "ref": "broker"},
            {"role": "lender", "ref": "lender-A"},
        ], "sign:m1")
        clock.advance(timedelta(minutes=1))
        service.disburse("m1", "83.00", "lender-A", "dis:m1")
        service.charge_fee("m1", "service_fee", "7.00", "lender-A", "fee:m1")
        return service

    def test_consumer_explanation_links_debt_cost_and_parties(self) -> None:
        service = self._populated()
        view = consumer_explanation(service.ledger.for_case("m1"))
        self.assertEqual(view["消费金额"], "88.00")
        self.assertEqual(view["真实成本"]["综合融资成本(含全部费用)"], Decimal("90.00"))
        self.assertTrue(view["决定是否分离"])
        self.assertEqual(len(view["两个独立决定"]), 2)
        roles = {p["角色"] for p in view["合同与参与方"]["参与方"]}
        self.assertEqual(roles, {"payment_platform", "loan_facilitator", "lender"})
        self.assertIn("放款方", view["为何形成债务"])

    def test_audit_timeline_as_of_reconstructs_history(self) -> None:
        from datetime import datetime
        service = self._populated()
        events = service.ledger.for_case("m1")
        full = audit_timeline(events)
        self.assertTrue(any(row["事件"] == "FUNDS_DISBURSED" for row in full))
        cutoff = datetime.fromisoformat("2026-09-26T10:00:30+08:00")
        early = audit_timeline(events, as_of=cutoff)
        self.assertFalse(any(row["事件"] == "FUNDS_DISBURSED" for row in early))
        # 每个界面展示环节都带界面指纹
        for row in early:
            if row["事件"] in {"INTENT_CREATED", "PAYMENT_CONFIRMED", "OFFER_PRESENTED"}:
                self.assertIn("界面指纹", row)

    def test_lender_view_is_minimized_and_partitioned(self) -> None:
        service = self._populated()
        events = service.ledger.for_case("m1")
        view = lender_view(events, "lender-A")
        # 只能看到适当性输入哈希，看不到原始收入数据
        self.assertIn("输入哈希", view["适当性"])
        serialized = repr(view)
        self.assertNotIn("10000", serialized)  # 月收入原始值不可见
        self.assertNotIn("promo-instant", serialized)
        # 非本机构放款方不可见
        with self.assertRaises(PermissionError):
            lender_view(events, "lender-B")


if __name__ == "__main__":
    unittest.main()
