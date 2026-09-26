"""事件归约：把一条业务键上的事件流折叠为当前状态。

状态只是事件的派生结果，不单独持久化；重启后重新归约即可恢复，
因此阶段（犹豫期截止、账单到期、逾期、异议窗关闭）不会因重启而重置。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def money(value: Any) -> Decimal:
    return Decimal(str(value))


@dataclass
class Consent:
    kind: str
    action_ref: str
    offer_version: str
    ui_hash: str
    captured_at: datetime
    withdrawn: bool = False


@dataclass
class Bill:
    seq: int
    amount_due: Decimal
    currency: str
    period_start: str
    period_end: str
    due_at: datetime
    settled: Decimal = Decimal("0")
    due_noticed: bool = False
    overdue_noticed: bool = False
    window_closed: bool = False
    rejected_debits: set[str] = field(default_factory=set)

    @property
    def outstanding(self) -> Decimal:
        return self.amount_due - self.settled


@dataclass
class BusinessState:
    business_key: str
    intent: Optional[dict[str, Any]] = None
    payment_confirmed: Optional[dict[str, Any]] = None
    offers: list[dict[str, Any]] = field(default_factory=list)
    suitability: Optional[dict[str, Any]] = None
    consents: dict[str, Consent] = field(default_factory=dict)
    line: Optional[dict[str, Any]] = None
    released: Decimal = Decimal("0")
    agreement: Optional[dict[str, Any]] = None
    pending_reconfirmation: bool = False
    disbursed: Decimal = Decimal("0")
    disbursements: list[dict[str, Any]] = field(default_factory=list)
    fees: list[dict[str, Any]] = field(default_factory=list)
    bills: dict[int, Bill] = field(default_factory=dict)
    debits: dict[str, dict[str, Any]] = field(default_factory=dict)  # idempotency_key -> 结果
    collection_case: Optional[dict[str, Any]] = None
    notices: list[dict[str, Any]] = field(default_factory=list)
    disputes: list[dict[str, Any]] = field(default_factory=list)
    frozen: bool = False
    freeze_reason: Optional[str] = None
    withdrawn: bool = False
    cooling_off_expired: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)

    # ---- 派生判断 ----

    @property
    def current_offer(self) -> Optional[dict[str, Any]]:
        return self.offers[-1] if self.offers else None

    def has_explicit_consent(self, kind: str) -> bool:
        consent = self.consents.get(kind)
        return consent is not None and not consent.withdrawn

    def consent_matches_offer(self, kind: str) -> bool:
        consent = self.consents.get(kind)
        return (
            consent is not None
            and not consent.withdrawn
            and self.current_offer is not None
            and consent.offer_version == self.current_offer["offer_version"]
        )

    @property
    def credit_available(self) -> Decimal:
        if self.line is None:
            return Decimal("0")
        return money(self.line["limit"]) - self.disbursed - self.released

    @classmethod
    def from_events(cls, business_key: str, events: list[dict[str, Any]]) -> "BusinessState":
        state = cls(business_key=business_key)
        for event in sorted(events, key=lambda e: (parse_ts(e["occurred_at"]), e["event_id"])):
            state.apply(event)
        return state

    def apply(self, event: dict[str, Any]) -> None:
        p = event.get("payload", {})
        kind = event["event_type"]
        self.events.append(event)

        if kind == "INTENT_CREATED":
            self.intent = p
        elif kind == "PAYMENT_CONFIRMED":
            self.payment_confirmed = p
            # 支付确认同样是一次显式授权决定，登记进授权表便于统一核验。
            self.consents["payment_confirmation"] = Consent(
                kind="payment_confirmation",
                action_ref=p["action_ref"],
                offer_version="",
                ui_hash=p["ui_hash"],
                captured_at=parse_ts(event["occurred_at"]),
            )
        elif kind == "OFFER_PRESENTED":
            self.offers.append(p)
        elif kind == "SUITABILITY_EVALUATED":
            self.suitability = p
        elif kind == "CONSENT_CAPTURED":
            self.consents[p["consent_kind"]] = Consent(
                kind=p["consent_kind"],
                action_ref=p["action_ref"],
                offer_version=p["offer_version"],
                ui_hash=p["ui_hash"],
                captured_at=parse_ts(event["occurred_at"]),
            )
        elif kind == "CONSENT_WITHDRAWN":
            self.withdrawn = True
            # 信贷授权事实保留（可审计），但信贷同意标记为失效；支付确认是既成事实不撤销。
            credit = self.consents.get("credit_consent")
            if credit is not None:
                credit.withdrawn = True
        elif kind == "CREDIT_LINE_OPENED":
            self.line = p
        elif kind == "CREDIT_LIMIT_RELEASED":
            self.released += money(p["amount"])
        elif kind == "AGREEMENT_SIGNED":
            self.agreement = p
            self.pending_reconfirmation = False
        elif kind == "AGREEMENT_TERMS_CHANGED":
            self.pending_reconfirmation = True
        elif kind == "AGREEMENT_RECONFIRMED":
            self.agreement = {**(self.agreement or {}), "offer_version": p["new_offer_version"]}
            self.pending_reconfirmation = False
        elif kind == "FUNDS_DISBURSED":
            self.disbursed += money(p["amount"])
            self.disbursements.append(p)
        elif kind == "FEE_CHARGED":
            self.fees.append(p)
        elif kind == "BILL_ISSUED":
            self.bills[p["bill_seq"]] = Bill(
                seq=p["bill_seq"],
                amount_due=money(p["amount_due"]),
                currency=p["currency"],
                period_start=p["period_start"],
                period_end=p["period_end"],
                due_at=parse_ts(p["due_at"]),
            )
        elif kind == "DEBIT_SETTLED":
            bill = self.bills[p["bill_seq"]]
            bill.settled += money(p["amount"])
            self.debits[p["idempotency_key"]] = {"outcome": "settled", **p}
        elif kind == "DEBIT_REJECTED":
            self.bills[p["bill_seq"]].rejected_debits.add(p["idempotency_key"])
            self.debits[p["idempotency_key"]] = {"outcome": "rejected", **p}
        elif kind == "COOLING_OFF_EXPIRED":
            self.cooling_off_expired = True
        elif kind == "BILL_DUE":
            self.bills[p["bill_seq"]].due_noticed = True
        elif kind == "ACCOUNT_OVERDUE":
            self.bills[p["bill_seq"]].overdue_noticed = True
        elif kind == "DISPUTE_WINDOW_CLOSED":
            self.bills[p["bill_seq"]].window_closed = True
        elif kind == "COLLECTION_CASE_OPENED":
            self.collection_case = p
        elif kind == "COLLECTION_NOTICE_SENT":
            self.notices.append(p)
        elif kind == "DISPUTE_OPENED":
            self.disputes.append(p)
        elif kind == "DISPUTE_RESOLVED":
            if self.disputes:
                self.disputes[-1] = {**self.disputes[-1], "resolution": p["resolution"], "resolved_at": p["resolved_at"]}
        elif kind == "FREEZE_RAISED":
            self.frozen = True
            self.freeze_reason = p["reason"]
        elif kind == "FREEZE_LIFTED":
            self.frozen = False
            self.freeze_reason = None
