import json
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from credit_consent.clock import ControlledClock
from credit_consent.domain import PaymentCreditService
from credit_consent.ledger import Ledger


def load_schema() -> dict:
    return json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))


def build_service(path: str | None = None, start=None) -> tuple[Ledger, ControlledClock, PaymentCreditService]:
    ledger = Ledger(path=path, schema=load_schema())
    clock = ControlledClock(start)
    service = PaymentCreditService(ledger, clock)
    return ledger, clock, service


def publish_default_rule(service: PaymentCreditService) -> None:
    service.publish_suitability_rule(
        "affordability",
        "rule-v1",
        "compliance_officer",
        {"max_payment_to_income": "0.5", "max_institutions": 3},
    )


def happy_case(service: PaymentCreditService, case_id: str = "case-001", *, credit: bool = True) -> dict:
    """走通：意图→优惠→支付确认→要约→适当性→（信贷同意）。返回各业务键。"""
    publish_default_rule(service)
    keys = {}
    keys["intent"] = f"intent:{case_id}"
    service.create_intent(case_id, "88.00", "CNY", "ui-v1", keys["intent"])
    service.apply_promo(case_id, "promo-instant-5", "签约月付立减5元（营销条件，非授权）", "5.00", "ui-v1")
    keys["pay"] = f"pay:{case_id}"
    service.confirm_payment(case_id, "btn-pay-001", "ui-v1", keys["pay"])
    service.present_offer(
        case_id,
        "offer-v1",
        "ui-credit-v1",
        principal="83.00",
        total_cost="90.00",
        apr="0.18",
        fee_breakdown=[{"type": "service_fee", "amount": "7.00"}],
        repayment_schedule=[{"due": "2026-10-26", "amount": "90.00"}],
        lender_ref="lender-A",
        terms={"apr": "0.18", "parties": ["lender-A"], "principal": "83.00"},
    )
    service.assess_suitability(
        case_id,
        "offer-v1",
        {"monthly_income": "10000", "monthly_payment": "2000", "existing_credit_institutions": []},
    )
    if credit:
        keys["credit"] = f"credit:{case_id}"
        service.capture_credit_consent(case_id, "switch-monthly-001", "offer-v1", "ui-credit-v1", keys["credit"])
    return keys
