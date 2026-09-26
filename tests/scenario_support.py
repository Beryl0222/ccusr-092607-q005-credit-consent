"""全链路场景测试的公共构造。"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from credit_consent.clock import ControllableClock
from credit_consent.ledger import Ledger
from credit_consent.service import CreditConsentService, TimelinePolicy
from credit_consent.storage import AppendOnlyLog

UI_HASH = "ui-v-7f3a"
OFFER_V1 = "OFFER-2026-V1"
LENDER = "LENDER-NO7"
RULESET = "RULES-2026-V3"


def load_schema() -> dict:
    return json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))


def build_service(tmp_path: Path, policy: TimelinePolicy | None = None):
    log = AppendOnlyLog(tmp_path / "ledger.jsonl")
    clock = ControllableClock()
    ledger = Ledger(log, load_schema(), clock)
    return CreditConsentService(ledger, policy), ledger, log


def approved_ruleset(svc: CreditConsentService) -> None:
    svc.draft_ruleset(RULESET, actor_role="marketing")
    svc.approve_ruleset(RULESET, actor_role="compliance")


def onboarded_business(svc: CreditConsentService, bk: str = "BK-001", *, decision: str = "approved"):
    """走到合同签署完成、随时可放款的状态。"""
    svc.publish_ui_version(UI_HASH, "content-sha256-abc")
    approved_ruleset(svc)
    svc.create_intent(
        bk,
        merchant_ref="MERCHANT-88",
        amount="199.00",
        currency="CNY",
        incentive_snapshot={
            "incentive_id": "INSTANT-15",
            "description": "首单立减15元",
            "discount_amount": "15.00",
            "conditions": "需开通月付",
        },
    )
    svc.present_offer(
        bk,
        ui_hash=UI_HASH,
        offer_version=OFFER_V1,
        lender_ref=LENDER,
        principal="199.00",
        total_cost="215.40",
        apr="0.1825",
        fee_schedule=[{"fee_type": "service_fee", "amount": "16.40", "currency": "CNY"}],
    )
    svc.confirm_payment(bk, action_ref="tap-pay-0001", ui_hash=UI_HASH)
    svc.evaluate_suitability(
        bk,
        ruleset_version=RULESET,
        input_fields={"monthly_income": "9000", "existing_debt_ratio": "0.21"},
        inputs_hash="inputs-sha256-001",
        decision=decision,
        actor_role="compliance",
    )
    svc.capture_credit_consent(bk, action_ref="separate-credit-toggle-0002")
    svc.open_credit_line(bk, limit="5000.00", currency="CNY")
    svc.sign_agreement(
        bk,
        parties=[
            {"role": "platform", "ref": "PAY-PLATFORM", "name": "支付平台"},
            {"role": "loan_facilitator", "ref": "FAC-X", "name": "助贷机构"},
            {"role": "lender", "ref": LENDER, "name": "第七消费金融"},
        ],
    )
    return bk
