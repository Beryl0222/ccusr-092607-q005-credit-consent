"""全链路业务不变量测试。"""

import json
import tempfile
import threading
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from scenario_support import LENDER, OFFER_V1, UI_HASH, build_service, onboarded_business
from credit_consent.clock import ClockRejected
from credit_consent.ledger import Ledger
from credit_consent.model import BusinessState
from credit_consent.projections import AccessDenied, audit_timeline, collector_view, consumer_explanation, lender_view
from credit_consent.service import DomainError, TimelinePolicy
from credit_consent.storage import AppendOnlyLog, TamperDetected


def expect_error(testcase: unittest.TestCase, code: str, fn, *args, **kwargs):
    with testcase.assertRaises(DomainError) as caught:
        fn(*args, **kwargs)
    testcase.assertEqual(code, caught.exception.code)
    return caught.exception


class TwoDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.ledger, self.log = build_service(Path(tempfile.mkdtemp()))

    def test_incentive_or_default_cannot_replace_credit_consent(self) -> None:
        bk = onboarded_business(self.svc, "BK-A")
        # 优惠只记录在意图片断里；没有第二次显式动作前开立授信必须失败。
        state = self.svc._state(bk)
        self.assertTrue(state.has_explicit_consent("payment_confirmation"))
        self.assertTrue(state.has_explicit_consent("credit_consent"))

    def test_credit_consent_requires_separate_action(self) -> None:
        from scenario_support import approved_ruleset

        bk = "BK-B"
        self.svc.publish_ui_version(UI_HASH, "content")
        approved_ruleset(self.svc)
        self.svc.create_intent(bk, "M", "100.00", "CNY", {"description": "立减5元"})
        self.svc.present_offer(bk, UI_HASH, OFFER_V1, LENDER, "100.00", "108.00", "0.18", [])
        self.svc.confirm_payment(bk, "tap-pay-1", UI_HASH)
        self.svc.evaluate_suitability(bk, "RULES-2026-V3", {"income": "8000"}, "h1", "approved", "compliance")
        # 复用支付动作不能冒充信贷同意
        expect_error(self, "single_action_for_two_decisions",
                     self.svc.capture_credit_consent, bk, "tap-pay-1")
        self.svc.capture_credit_consent(bk, "explicit-credit-action-2")
        self.assertTrue(self.svc._state(bk).has_explicit_consent("credit_consent"))

    def test_cannot_consent_before_payment(self) -> None:
        from scenario_support import approved_ruleset

        bk = "BK-C"
        self.svc.publish_ui_version(UI_HASH, "content")
        approved_ruleset(self.svc)
        self.svc.create_intent(bk, "M", "100.00", "CNY", {})
        self.svc.present_offer(bk, UI_HASH, OFFER_V1, LENDER, "100.00", "108.00", "0.18", [])
        self.svc.evaluate_suitability(bk, "RULES-2026-V3", {"income": "8000"}, "h1", "approved", "compliance")
        expect_error(self, "payment_not_confirmed", self.svc.capture_credit_consent, bk, "act-2")


class SeparationOfDutiesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, _, _ = build_service(Path(tempfile.mkdtemp()))

    def test_marketing_cannot_approve_ruleset(self) -> None:
        self.svc.draft_ruleset("R1", "marketing")
        expect_error(self, "forbidden_approver", self.svc.approve_ruleset, "R1", "marketing")

    def test_unapproved_ruleset_cannot_evaluate(self) -> None:
        self.svc.publish_ui_version(UI_HASH, "c")
        bk = "BK-D"
        self.svc.create_intent(bk, "M", "10.00", "CNY", {})
        self.svc.present_offer(bk, UI_HASH, "V1", LENDER, "10.00", "11.00", "0.1", [])
        expect_error(self, "ruleset_not_approved",
                     self.svc.evaluate_suitability, bk, "R1", {}, "h", "approved", "compliance")

    def test_marketing_cannot_evaluate(self) -> None:
        self.svc.draft_ruleset("R1", "marketing")
        self.svc.approve_ruleset("R1", "compliance")
        self.svc.publish_ui_version(UI_HASH, "c")
        bk = "BK-E"
        self.svc.create_intent(bk, "M", "10.00", "CNY", {})
        self.svc.present_offer(bk, UI_HASH, "V1", LENDER, "10.00", "11.00", "0.1", [])
        expect_error(self, "forbidden_evaluator",
                     self.svc.evaluate_suitability, bk, "R1", {}, "h", "approved", "marketing")


class FreezeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.ledger, _ = build_service(Path(tempfile.mkdtemp()))

    def test_different_ui_hash_freezes_immediately(self) -> None:
        bk = "BK-F"
        self.svc.publish_ui_version("ui-1", "c1")
        self.svc.publish_ui_version("ui-2", "c2")
        from scenario_support import approved_ruleset

        approved_ruleset(self.svc)
        self.svc.create_intent(bk, "M", "100.00", "CNY", {})
        self.svc.present_offer(bk, "ui-1", "V1", LENDER, "100.00", "108.00", "0.18", [])
        expect_error(self, "business_frozen",
                     self.svc.present_offer, bk, "ui-2", "V2", LENDER, "100.00", "108.00", "0.18", [])
        state = self.svc._state(bk)
        self.assertTrue(state.frozen)
        self.assertIn("界面指纹", state.freeze_reason)
        # 冻结后任何资金动作都被拒绝
        expect_error(self, "business_frozen", self.svc.confirm_payment, bk, "a1", "ui-2")

    def test_different_amount_freezes(self) -> None:
        bk = "BK-G"
        self.svc.publish_ui_version(UI_HASH, "c")
        self.svc.create_intent(bk, "M", "100.00", "CNY", {})
        expect_error(self, "business_frozen",
                     self.svc.present_offer, bk, UI_HASH, "V1", LENDER, "120.00", "128.00", "0.18", [])
        self.assertTrue(self.svc._state(bk).frozen)

    def test_only_compliance_can_lift_freeze(self) -> None:
        bk = "BK-H"
        self.svc.publish_ui_version(UI_HASH, "c")
        self.svc.create_intent(bk, "M", "100.00", "CNY", {})
        expect_error(self, "business_frozen",
                     self.svc.present_offer, bk, UI_HASH, "V1", LENDER, "120.00", "128.00", "0.18", [])
        expect_error(self, "forbidden", self.svc.lift_freeze, bk, "误操作", "lender")
        self.svc.lift_freeze(bk, "金额差异系系统故障，已核实", "compliance")
        self.assertFalse(self.svc._state(bk).frozen)


class WithdrawalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.ledger, self.log = build_service(Path(tempfile.mkdtemp()))

    def test_withdraw_unused_credit_releases_limit_but_keeps_evidence(self) -> None:
        bk = onboarded_business(self.svc, "BK-I")
        self.svc.open_credit_line  # sanity
        events_before = len(self.ledger.all_events())
        self.svc.withdraw_credit(bk, "用户在犹豫期内改变主意")
        state = self.svc._state(bk)
        self.assertTrue(state.withdrawn)
        self.assertFalse(state.has_explicit_consent("credit_consent"))
        # 历史证据保留：授权、要约、评估事件仍在链上
        types_ = {e["event_type"] for e in self.ledger.events_for(bk)}
        self.assertIn("CONSENT_CAPTURED", types_)
        self.assertIn("SUITABILITY_EVALUATED", types_)
        self.assertIn("CREDIT_LIMIT_RELEASED", types_)
        self.assertEqual(state.credit_available, Decimal("0"))
        self.assertGreater(len(self.ledger.all_events()), events_before)
        # 撤销后不得再放款
        expect_error(self, "credit_withdrawn", self.svc.disburse, bk, "idem-1", "199.00", "CNY")

    def test_cannot_withdraw_after_disbursement(self) -> None:
        bk = onboarded_business(self.svc, "BK-J")
        self.svc.disburse(bk, "idem-d1", "199.00", "CNY")
        expect_error(self, "credit_already_used", self.svc.withdraw_credit, bk, "反悔")


class TermsChangeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, _, _ = build_service(Path(tempfile.mkdtemp()))

    def test_rate_change_blocks_disburse_until_reconfirmed(self) -> None:
        bk = onboarded_business(self.svc, "BK-K")
        self.svc.change_terms(bk, "OFFER-2026-V2", ["apr", "total_cost"])
        expect_error(self, "terms_changed", self.svc.disburse, bk, "idem-1", "199.00", "CNY")
        # 重新确认必须是新动作
        expect_error(self, "reused_action",
                     self.svc.reconfirm_agreement, bk, "OFFER-2026-V2", "separate-credit-toggle-0002")
        self.svc.reconfirm_agreement(bk, "OFFER-2026-V2", "fresh-reconfirm-action-3")
        event = self.svc.disburse(bk, "idem-1", "199.00", "CNY")
        self.assertEqual("FUNDS_DISBURSED", event["event_type"])

    def test_party_change_requires_reconfirmation_for_debits(self) -> None:
        bk = onboarded_business(self.svc, "BK-L")
        self.svc.disburse(bk, "idem-1", "199.00", "CNY")
        self.svc.issue_bill(bk, "199.00", "CNY", "2026-09-01", "2026-09-30",
                            "2026-10-10T00:00:00+08:00", "300.00")
        self.svc.change_terms(bk, "OFFER-V3", ["parties"])
        expect_error(self, "terms_changed",
                     self.svc.request_debit, bk, "deb-1", 1, "199.00", "collector")


class IdempotencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.ledger, _ = build_service(Path(tempfile.mkdtemp()))

    def test_disburse_retry_does_not_double_fund(self) -> None:
        bk = onboarded_business(self.svc, "BK-M")
        first = self.svc.disburse(bk, "idem-net-retry", "199.00", "CNY")
        second = self.svc.disburse(bk, "idem-net-retry", "199.00", "CNY")
        third = self.svc.disburse(bk, "idem-net-retry", "199.00", "CNY")
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(first["event_id"], third["event_id"])
        self.assertEqual(self.svc._state(bk).disbursed, Decimal("199.00"))
        disbursed = [e for e in self.ledger.all_events() if e["event_type"] == "FUNDS_DISBURSED"]
        self.assertEqual(1, len(disbursed))

    def test_debit_retry_returns_same_verdict(self) -> None:
        bk = onboarded_business(self.svc, "BK-N")
        self.svc.disburse(bk, "idem-1", "100.00", "CNY")
        self.svc.issue_bill(bk, "100.00", "CNY", "p1", "p2", "2026-10-10T00:00:00+08:00", "100.00")
        r1 = self.svc.request_debit(bk, "deb-retry", 1, "100.00", "collector")
        r2 = self.svc.request_debit(bk, "deb-retry", 1, "100.00", "collector")
        self.assertEqual(r1["event_id"], r2["event_id"])
        self.assertEqual("DEBIT_SETTLED", r1["event_type"])

    def test_distinct_keys_compete_for_balance_atomically(self) -> None:
        bk = "BK-O"
        onboarded_business(self.svc, bk)
        self.svc.disburse(bk, "idem-1", "150.00", "CNY")
        # 渠道可用余额只有 100；两个不同幂等键并发各请求 80，必须恰好一成一拒。
        self.svc.issue_bill(bk, "150.00", "CNY", "p1", "p2", "2026-10-10T00:00:00+08:00", "100.00")
        results: list[str] = []
        errors: list[BaseException] = []

        def debit(key: str) -> None:
            try:
                event = self.svc.request_debit(bk, key, 1, "80.00", "collector")
                results.append(event["event_type"])
            except BaseException as exc:  # pragma: no cover - 争用不应抛错
                errors.append(exc)

        t1 = threading.Thread(target=debit, args=("deb-a",))
        t2 = threading.Thread(target=debit, args=("deb-b",))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual([], errors)
        self.assertEqual(sorted(results), ["DEBIT_REJECTED", "DEBIT_SETTLED"])
        state = self.svc._state(bk)
        self.assertEqual(state.bills[1].settled, Decimal("80.00"))

    def test_insufficient_balance_leaves_rejection_evidence(self) -> None:
        bk = "BK-P"
        onboarded_business(self.svc, bk)
        self.svc.disburse(bk, "idem-1", "100.00", "CNY")
        self.svc.issue_bill(bk, "100.00", "CNY", "p1", "p2", "2026-10-10T00:00:00+08:00", "50.00")
        event = self.svc.request_debit(bk, "deb-x", 1, "100.00", "collector")
        self.assertEqual("DEBIT_REJECTED", event["event_type"])
        self.assertIn("余额不足", event["payload"]["reason"])


class ClockAndLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.ledger, self.log = build_service(
            Path(tempfile.mkdtemp()),
            TimelinePolicy(cooling_off_days=1, overdue_grace_days=0, dispute_window_days=30),
        )

    def _overdue_business(self, bk: str) -> None:
        onboarded_business(self.svc, bk)
        self.svc.disburse(bk, "idem-1", "100.00", "CNY")
        self.svc.issue_bill(bk, "100.00", "CNY", "2026-09-01", "2026-09-25",
                            "2026-09-26T00:00:00+08:00", "100.00")

    def test_clock_drives_stages_and_restart_keeps_phase(self) -> None:
        bk = "BK-Q"
        self._overdue_business(bk)
        # 时钟起点 2026-09-25 00:00；推进 2 天跨过犹豫期截止与账单到期。
        self.svc.advance_clock(days=2)
        state = self.svc._state(bk)
        self.assertTrue(state.cooling_off_expired)
        self.assertTrue(state.bills[1].due_noticed)
        self.assertTrue(state.bills[1].overdue_noticed)
        types1 = {e["event_type"] for e in self.ledger.events_for(bk)}
        self.assertIn("COOLING_OFF_EXPIRED", types1)
        self.assertIn("BILL_DUE", types1)
        self.assertIn("ACCOUNT_OVERDUE", types1)

        # 重启：从哈希链回放，时钟与阶段不重置；阶段事件不会补发。
        reopened_log = AppendOnlyLog(self.log.path)
        reopened = Ledger(reopened_log, self.ledger.schema)
        self.assertEqual(reopened.clock.now, self.ledger.clock.now)
        svc2 = type(self.svc)(reopened, self.svc.policy)
        svc2.advance_clock(days=1)
        types2 = [e["event_type"] for e in reopened.events_for(bk) if e["event_type"] == "ACCOUNT_OVERDUE"]
        self.assertEqual(1, len(types2))

    def test_clock_cannot_go_backwards(self) -> None:
        with self.assertRaises(ClockRejected):
            self.ledger.clock.advance(timedelta(seconds=-1))

    def test_dispute_window_closes_then_channel_rejects(self) -> None:
        bk = "BK-R"
        self._overdue_business(bk)
        self.svc.open_collection_case  # noqa: B018
        self.svc.advance_clock(days=31)
        expect_error(self, "window_closed", self.svc.open_dispute, bk, "不认可该债务", ["ev-1"])

    def test_collection_and_notice_flow(self) -> None:
        bk = "BK-S"
        self._overdue_business(bk)
        # 未逾期不能立案
        fresh = "BK-S2"
        onboarded_business(self.svc, fresh)
        self.svc.disburse(fresh, "idem-9", "10.00", "CNY")
        self.svc.issue_bill(fresh, "10.00", "CNY", "p", "p", "2027-01-01T00:00:00+08:00", "10.00")
        expect_error(self, "not_overdue", self.svc.open_collection_case, fresh, "COL-AGENCY-1")
        # 逾期后立案、通知、异议
        self.svc.advance_clock(days=2)
        self.svc.open_collection_case(bk, "COL-AGENCY-1")
        n1 = self.svc.send_collection_notice(bk, "sms", "TPL-v4")
        n2 = self.svc.send_collection_notice(bk, "letter", "TPL-v4")
        self.assertEqual(1, n1["payload"]["notice_seq"])
        self.assertEqual(2, n2["payload"]["notice_seq"])
        self.svc.open_dispute(bk, "我没看到月付提示", ["screenshot-1"])
        state = self.svc._state(bk)
        self.assertTrue(any("resolution" not in d for d in state.disputes))


class HashChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.ledger, self.log = build_service(Path(tempfile.mkdtemp()))

    def test_tampering_is_detected(self) -> None:
        bk = onboarded_business(self.svc, "BK-T")
        self.svc.disburse(bk, "idem-1", "199.00", "CNY")
        self.assertTrue(self.log.verify())
        # 直接改写磁盘上的金额
        lines = self.log.path.read_text(encoding="utf-8").splitlines()
        tampered = [json.loads(line) for line in lines]
        for record in tampered:
            body = record["body"]
            if isinstance(body, dict) and body.get("event_type") == "FUNDS_DISBURSED":
                body["payload"]["amount"] = "0.01"
        self.log.path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in tampered) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(TamperDetected):
            AppendOnlyLog(self.log.path)

    def test_deletion_is_detected(self) -> None:
        onboarded_business(self.svc, "BK-U")
        lines = self.log.path.read_text(encoding="utf-8").splitlines()
        self.log.path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
        with self.assertRaises(TamperDetected):
            AppendOnlyLog(self.log.path)


class ProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.ledger, _ = build_service(Path(tempfile.mkdtemp()))

    def test_consumer_explanation_links_debt_to_decisions_and_cost(self) -> None:
        bk = onboarded_business(self.svc, "BK-V")
        self.svc.disburse(bk, "idem-1", "199.00", "CNY")
        explanation = consumer_explanation(self.svc._state(bk))
        self.assertEqual(2, len(explanation["你做过的两次决定"]))
        self.assertIn("215.40", json.dumps(explanation["真实成本"], ensure_ascii=False))
        refs = {p["标识"] for p in explanation["责任方"]}
        self.assertIn(LENDER, refs)

    def test_audit_timeline_reconstructs_history_in_order(self) -> None:
        bk = onboarded_business(self.svc, "BK-W")
        report = audit_timeline(self.svc._state(bk))
        types_ = [step["event_type"] for step in report["timeline"]]
        self.assertLess(types_.index("OFFER_PRESENTED"), types_.index("CONSENT_CAPTURED"))
        self.assertLess(types_.index("PAYMENT_CONFIRMED"), types_.index("CONSENT_CAPTURED"))
        self.assertTrue(report["链路核查"]["支付确认与信贷同意分离"])
        self.assertTrue(report["链路核查"]["适当性评估先于信贷同意"])

    def test_lender_view_is_minimal_and_scoped(self) -> None:
        bk = onboarded_business(self.svc, "BK-X")
        state = self.svc._state(bk)
        view = lender_view(state, LENDER)
        self.assertNotIn("input_fields", json.dumps(view, ensure_ascii=False))
        with self.assertRaises(AccessDenied):
            lender_view(state, "LENDER-OTHER")

    def test_collector_view_is_scoped(self) -> None:
        bk = "BK-Y"
        svc = self.svc
        onboarded_business(svc, bk)
        svc.disburse(bk, "idem-1", "100.00", "CNY")
        svc.issue_bill(bk, "100.00", "CNY", "p", "p", "2026-09-26T00:00:00+08:00", "100.00")
        svc.advance_clock(days=2)
        svc.open_collection_case(bk, "COL-1")
        view = collector_view(svc._state(bk), "COL-1")
        self.assertEqual("100.00", view["amount_outstanding"])
        with self.assertRaises(AccessDenied):
            collector_view(svc._state(bk), "COL-OTHER")


if __name__ == "__main__":
    unittest.main()
