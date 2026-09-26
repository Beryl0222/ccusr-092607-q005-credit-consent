"""穿透账领域服务：把基础事件组装成受规则约束的业务链路。

关键不变量：
- 支付确认与信贷同意是两个独立、显式的决定，默认值与优惠不构成授权；
- 适当性规则只能由合规角色批准，营销配置人员无权批准；
- 合同主体或费率变化必须重新确认，旧要约版本上的同意不再可用；
- 撤销未使用的授信释放额度但保留全部历史证据；
- 同一业务键重试绝不产生第二次签约/放款/扣款；同键不同指纹或金额立即冻结；
- 扣款争用同一可用余额时，在账本临界区内原子完成检查与落账；
- 犹豫期、账单、逾期、异议期限全部由可控时钟推进，阶段从事件流重建。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Mapping

from .clock import ControlledClock
from .ledger import Ledger, StoredEvent, canonical_hash, make_event

# 可批准适当性规则的角色；营销配置不在其中。
RULE_APPROVER_ROLES = frozenset({"compliance_officer"})
SUITABILITY_ASSESSOR_ROLES = frozenset({"suitability_engine", "compliance_officer", "manual_reviewer"})

COOLING_OFF_DAYS = 7
DISPUTE_WINDOW_DAYS = 30

PHASE_INITIATED = "initiated"
PHASE_CAPTURED = "captured"
PHASE_COOLING_OFF = "cooling_off"
PHASE_ACTIVE = "active"
PHASE_BILLING = "billing"
PHASE_OVERDUE = "overdue"
PHASE_DISPUTE = "dispute"
PHASE_SETTLED = "settled"
PHASE_REVOKED = "revoked"
PHASE_FROZEN = "frozen"


class DomainError(RuntimeError):
    """领域规则被违反。"""


class RuleGovernanceError(DomainError):
    """无权批准或引用未经批准的适当性规则。"""


class ConsentError(DomainError):
    """授权不成立（非显式、缺少前置决定、版本失效等）。"""


class FrozenCaseError(DomainError):
    """案件已冻结，停止一切签约、放款与扣款。"""


class InvalidStateError(DomainError):
    """当前阶段不允许该操作。"""


def D(value: str | Decimal) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


# ---------------------------------------------------------------------------
# 状态投影：从不可变事件流还原案件当前状态
# ---------------------------------------------------------------------------


@dataclass
class OfferState:
    offer_version: str
    ui_hash: str
    principal: Decimal
    total_cost: Decimal
    apr: str
    fee_breakdown: list[dict[str, Any]]
    repayment_schedule: list[dict[str, Any]]
    lender_ref: str
    terms_hash: str


@dataclass
class ConsentState:
    kind: str
    action_ref: str
    offer_version: str | None
    ui_hash: str
    terms_hash: str | None
    business_key: str


@dataclass
class CaseState:
    case_id: str
    phase: str = PHASE_INITIATED
    intent: dict[str, Any] | None = None
    intent_ref: str | None = None
    payment_confirmed: dict[str, Any] | None = None
    promo: dict[str, Any] | None = None
    offers: dict[str, OfferState] = field(default_factory=dict)
    latest_offer_version: str | None = None
    assessments: dict[str, dict[str, Any]] = field(default_factory=dict)
    consents: dict[str, ConsentState] = field(default_factory=dict)
    contract: dict[str, Any] | None = None
    reconfirmations: list[dict[str, Any]] = field(default_factory=list)
    revoked: dict[str, Any] | None = None
    disbursed: list[dict[str, Any]] = field(default_factory=list)
    fees: list[dict[str, Any]] = field(default_factory=list)
    bills: list[dict[str, Any]] = field(default_factory=list)
    deductions: list[dict[str, Any]] = field(default_factory=list)
    repayments: list[dict[str, Any]] = field(default_factory=list)
    notices: list[dict[str, Any]] = field(default_factory=list)
    disputes: list[dict[str, Any]] = field(default_factory=list)
    frozen_reasons: list[str] = field(default_factory=list)
    version: int = 0  # case_file 聚合版本

    @property
    def is_frozen(self) -> bool:
        return bool(self.frozen_reasons)

    def offer_version_alive(self, offer_version: str) -> bool:
        """该要约版本之后若发生过合同主体/费率变更，则版本失效。"""
        terms_hash = self.offers[offer_version].terms_hash
        for item in self.reconfirmations:
            if item["previous_terms_hash"] == terms_hash:
                return False
        return True


def project_case(events: list[Mapping[str, Any]]) -> CaseState:
    state: CaseState | None = None
    for raw in events:
        event = dict(raw)
        case_id = event["case_id"]
        if state is None:
            state = CaseState(case_id=case_id)
        body = event.get("payload", {})
        kind = event["event_type"]
        if kind == "INTENT_CREATED":
            state.intent = dict(body)
            state.intent_ref = body.get("intent_ref", f"intent-{case_id}")
        elif kind == "PROMO_APPLIED":
            state.promo = dict(body)
        elif kind == "PAYMENT_CONFIRMED":
            state.payment_confirmed = dict(body)
        elif kind == "OFFER_PRESENTED":
            state.offers[body["offer_version"]] = OfferState(
                offer_version=body["offer_version"],
                ui_hash=body["ui_hash"],
                principal=D(body["principal"]),
                total_cost=D(body["total_cost"]),
                apr=str(body["apr"]),
                fee_breakdown=list(body.get("fee_breakdown", [])),
                repayment_schedule=list(body.get("repayment_schedule", [])),
                lender_ref=body["lender_ref"],
                terms_hash=body["terms_hash"],
            )
            state.latest_offer_version = body["offer_version"]
        elif kind == "SUITABILITY_ASSESSED":
            state.assessments[body["offer_version"]] = dict(body)
        elif kind == "CONSENT_CAPTURED":
            state.consents[body["consent_kind"]] = ConsentState(
                kind=body["consent_kind"],
                action_ref=body["action_ref"],
                offer_version=body.get("offer_version"),
                ui_hash=body["ui_hash"],
                terms_hash=body.get("terms_hash"),
                business_key=body["business_key"],
            )
        elif kind == "CONTRACT_SIGNED":
            state.contract = dict(body)
        elif kind == "RECONFIRMATION_REQUIRED":
            state.reconfirmations.append(dict(body))
        elif kind == "CREDIT_REVOKED":
            state.revoked = dict(body)
        elif kind == "FUNDS_DISBURSED":
            state.disbursed.append(dict(body))
        elif kind == "FEE_CHARGED":
            state.fees.append(dict(body))
        elif kind == "REPAYMENT_DUE":
            state.bills.append(dict(body))
        elif kind == "DEDUCTION_REQUESTED":
            state.deductions.append(dict(body))
        elif kind == "REPAYMENT_MADE":
            state.repayments.append(dict(body))
        elif kind == "NOTICE_SENT":
            state.notices.append(dict(body))
        elif kind in ("DISPUTE_OPENED", "DISPUTE_RESOLVED"):
            state.disputes.append({"event_type": kind, **dict(body)})
        elif kind == "CASE_FROZEN":
            state.frozen_reasons.append(body["reason"])
        elif kind == "PHASE_ADVANCED":
            state.phase = body["to_phase"]
        if event["aggregate_type"] == "case_file":
            state.version = int(event["version"])
    if state is None:
        raise DomainError("案件不存在或尚无事件")
    return state


def project_balance(events: list[Mapping[str, Any]]) -> Decimal:
    """从余额登记事件与所有指向该账户的扣款事件重建可用余额（跨聚合投影）。"""
    balance: Decimal | None = None
    for event in events:
        body = event["payload"]
        if event["event_type"] == "BALANCE_REGISTERED":
            balance = D(body["available_amount"])
        elif event["event_type"] == "DEDUCTION_REQUESTED" and body["outcome"] == "settled":
            if balance is None:
                raise DomainError("扣款账户尚未登记可用余额")
            balance -= D(body["amount"])
    if balance is None:
        raise DomainError("扣款账户尚未登记可用余额")
    return balance


# ---------------------------------------------------------------------------
# 命令服务
# ---------------------------------------------------------------------------


class PaymentCreditService:
    def __init__(self, ledger: Ledger, clock: ControlledClock) -> None:
        self.ledger = ledger
        self.clock = clock
        self._rules: dict[str, dict[str, Any]] = {}
        self._replay_governance()

    def _replay_governance(self) -> None:
        for stored in self.ledger.records():
            event = stored.event
            if event["event_type"] == "SUITABILITY_RULE_PUBLISHED":
                self._rules[event["aggregate_id"]] = event["payload"]
    # ---- 通用辅助 -------------------------------------------------

    def _state(self, case_id: str) -> CaseState:
        return project_case(self.ledger.for_case(case_id))

    def _guard_active(self, state: CaseState) -> None:
        if state.is_frozen:
            raise FrozenCaseError(f"案件已冻结: {state.frozen_reasons}")

    def _next_version(self, aggregate_type: str, aggregate_id: str) -> int:
        return self.ledger.version_of(aggregate_type, aggregate_id) + 1

    def _append(self, **kwargs: Any) -> StoredEvent:
        return self.ledger.append(make_event(occurred_at=self.clock.now_iso(), **kwargs))

    def _phase(self, state: CaseState, to_phase: str) -> None:
        from_phase = state.phase
        self._append(
            event_type="PHASE_ADVANCED",
            aggregate_type="case_file",
            aggregate_id=state.case_id,
            case_id=state.case_id,
            version=self._next_version("case_file", state.case_id),
            payload={
                "from_phase": from_phase,
                "to_phase": to_phase,
                "effective_at": self.clock.now_iso(),
            },
        )
        state.phase = to_phase

    def _freeze(self, case_id: str, reason: str, evidence: Mapping[str, Any]) -> StoredEvent:
        state = self._state(case_id)
        stored = self._append(
            event_type="CASE_FROZEN",
            aggregate_type="case_file",
            aggregate_id=case_id,
            case_id=case_id,
            version=self._next_version("case_file", case_id),
            payload={"reason": reason, "evidence": dict(evidence)},
        )
        if state.phase != PHASE_FROZEN:
            self._phase(self._state(case_id), PHASE_FROZEN)
        return stored

    def _idempotency_check(
        self, case_id: str, business_key: str, fingerprint: str
    ) -> StoredEvent | None:
        """命中业务键：指纹一致则返回首次结果（幂等），不一致立即冻结。"""
        prior = self.ledger.get_by_business_key(business_key)
        if prior is None:
            return None
        prior_fp = prior.event.get("payload", {}).get("request_fingerprint")
        if prior_fp != fingerprint:
            amount_events = {"FUNDS_DISBURSED", "FEE_CHARGED", "DEDUCTION_REQUESTED"}
            reason = (
                "amount_conflict"
                if prior.event["event_type"] in amount_events
                else "fingerprint_conflict"
            )
            self._freeze(
                case_id,
                reason,
                {
                    "business_key": business_key,
                    "first_request_fingerprint": prior_fp,
                    "retry_request_fingerprint": fingerprint,
                    "first_event_type": prior.event["event_type"],
                },
            )
            raise FrozenCaseError(
                f"同一业务键 {business_key} 出现不同请求指纹，案件已冻结"
            )
        return prior

    # ---- 支付意图与优惠 -------------------------------------------

    def create_intent(
        self,
        case_id: str,
        amount: str,
        currency: str,
        ui_hash: str,
        business_key: str,
        intent_ref: str | None = None,
    ) -> StoredEvent:
        fingerprint = canonical_hash(("create_intent", amount, currency, ui_hash))
        prior = self._idempotency_check(case_id, business_key, fingerprint)
        if prior is not None:
            return prior
        intent_ref = intent_ref or f"intent-{case_id}"
        payload = {
            "business_key": business_key,
            "amount": str(D(amount)),
            "currency": currency,
            "ui_hash": ui_hash,
            "request_fingerprint": fingerprint,
        }
        return self._append(
            event_type="INTENT_CREATED",
            aggregate_type="payment_intent",
            aggregate_id=intent_ref,
            case_id=case_id,
            version=self._next_version("payment_intent", intent_ref),
            payload=payload,
            business_key=business_key,
        )

    def apply_promo(
        self,
        case_id: str,
        promo_ref: str,
        condition_text: str,
        discount_amount: str,
        ui_hash: str,
    ) -> StoredEvent:
        """优惠只是营销事实，永远不构成任何授权。"""
        state = self._state(case_id)
        self._guard_active(state)
        if state.intent_ref is None:
            raise InvalidStateError("优惠必须挂在已创建的支付意图上")
        intent_ref = state.intent_ref
        return self._append(
            event_type="PROMO_APPLIED",
            aggregate_type="payment_intent",
            aggregate_id=intent_ref,
            case_id=case_id,
            version=self._next_version("payment_intent", intent_ref),
            payload={
                "promo_ref": promo_ref,
                "condition_text": condition_text,
                "discount_amount": str(D(discount_amount)),
                "ui_hash": ui_hash,
            },
        )

    # ---- 决定一：支付确认 ------------------------------------------

    def confirm_payment(
        self, case_id: str, action_ref: str, ui_hash: str, business_key: str
    ) -> StoredEvent:
        state = self._state(case_id)
        self._guard_active(state)
        fingerprint = canonical_hash(("confirm_payment", action_ref, ui_hash))
        prior = self._idempotency_check(case_id, business_key, fingerprint)
        if prior is not None:
            return prior
        if state.payment_confirmed is not None:
            raise ConsentError("支付已确认，不得重复确认")
        intent_ref = state.intent_ref
        if intent_ref is None:
            raise InvalidStateError("支付意图不存在，无法确认支付")
        # 同一界面指纹：重试换皮（不同界面指纹）属于冲突。
        if state.intent.get("ui_hash") and ui_hash != state.intent["ui_hash"]:
            self._freeze(
                case_id,
                "fingerprint_conflict",
                {"business_key": business_key, "intent_ui_hash": state.intent["ui_hash"], "ui_hash": ui_hash},
            )
            raise FrozenCaseError("支付确认界面与意图界面指纹不一致，案件已冻结")
        with self.ledger.exclusive():
            stored = self._append(
                event_type="PAYMENT_CONFIRMED",
                aggregate_type="payment_intent",
                aggregate_id=intent_ref,
                case_id=case_id,
                version=self._next_version("payment_intent", intent_ref),
                payload={
                    "action_ref": action_ref,
                    "ui_hash": ui_hash,
                    "business_key": business_key,
                    "request_fingerprint": fingerprint,
                },
                business_key=business_key,
            )
            self._append(
                event_type="CONSENT_CAPTURED",
                aggregate_type="consent_record",
                aggregate_id=f"consent-{case_id}-payment_confirmation",
                case_id=case_id,
                version=self._next_version(
                    "consent_record", f"consent-{case_id}-payment_confirmation"
                ),
                payload={
                    "consent_kind": "payment_confirmation",
                    "action_ref": action_ref,
                    "offer_version": None,
                    "ui_hash": ui_hash,
                    "terms_hash": None,
                    "explicit": True,
                    "business_key": f"{business_key}:payment-consent",
                },
            )
        return stored

    # ---- 适当性治理 -----------------------------------------------

    def publish_suitability_rule(
        self, rule_id: str, rule_version: str, approved_by_role: str, rules: Mapping[str, Any]
    ) -> StoredEvent:
        if approved_by_role not in RULE_APPROVER_ROLES:
            raise RuleGovernanceError(
                f"角色 {approved_by_role} 无权批准适当性规则；只有 {sorted(RULE_APPROVER_ROLES)} 可以"
            )
        existing = self._rules.get(rule_id)
        if existing is not None and existing.get("rule_version") == rule_version:
            raise DomainError(f"规则版本 {rule_version} 已存在，规则只能以新版本发布")
        rules_hash = canonical_hash(rules)
        stored = self._append(
            event_type="SUITABILITY_RULE_PUBLISHED",
            aggregate_type="governance",
            aggregate_id=rule_id,
            version=self._next_version("governance", rule_id),
            payload={
                "rule_version": rule_version,
                "approved_by_role": approved_by_role,
                "rules_hash": rules_hash,
                "rules": dict(rules),
            },
        )
        self._rules[rule_id] = {
            "rule_version": rule_version,
            "rules_hash": rules_hash,
            "rules": dict(rules),
        }
        return stored

    def _latest_rule(self) -> tuple[str, dict[str, Any]]:
        if not self._rules:
            raise RuleGovernanceError("尚无经合规角色批准的适当性规则")
        rule_id = sorted(self._rules)[-1]
        return rule_id, self._rules[rule_id]

    def present_offer(
        self,
        case_id: str,
        offer_version: str,
        ui_hash: str,
        principal: str,
        total_cost: str,
        apr: str,
        fee_breakdown: list[Mapping[str, Any]],
        repayment_schedule: list[Mapping[str, Any]],
        lender_ref: str,
        terms: Mapping[str, Any],
    ) -> StoredEvent:
        state = self._state(case_id)
        self._guard_active(state)
        if offer_version in state.offers:
            raise DomainError(f"要约版本 {offer_version} 已展示，版本内容不可覆盖")
        terms_hash = canonical_hash(terms)
        offer_ref = f"offer-{case_id}"
        return self._append(
            event_type="OFFER_PRESENTED",
            aggregate_type="credit_offer",
            aggregate_id=offer_ref,
            case_id=case_id,
            version=self._next_version("credit_offer", offer_ref),
            payload={
                "offer_version": offer_version,
                "ui_hash": ui_hash,
                "principal": str(D(principal)),
                "total_cost": str(D(total_cost)),
                "apr": str(apr),
                "fee_breakdown": [dict(x) for x in fee_breakdown],
                "repayment_schedule": [dict(x) for x in repayment_schedule],
                "lender_ref": lender_ref,
                "terms_hash": terms_hash,
            },
        )

    def assess_suitability(
        self,
        case_id: str,
        offer_version: str,
        inputs: Mapping[str, Any],
        assessor_role: str = "suitability_engine",
        decision: str | None = None,
    ) -> StoredEvent:
        state = self._state(case_id)
        self._guard_active(state)
        if assessor_role not in SUITABILITY_ASSESSOR_ROLES:
            raise RuleGovernanceError(f"角色 {assessor_role} 不得执行适当性评估")
        offer = state.offers.get(offer_version)
        if offer is None:
            raise InvalidStateError("适当性评估必须针对已展示的要约版本")
        rule_id, rule = self._latest_rule()
        derived = self._evaluate_rules(rule["rules"], inputs) if decision is None else decision
        if derived not in {"eligible", "ineligible", "manual_review"}:
            raise DomainError("评估结论不合法")
        offer_ref = f"offer-{case_id}"
        return self._append(
            event_type="SUITABILITY_ASSESSED",
            aggregate_type="credit_offer",
            aggregate_id=offer_ref,
            case_id=case_id,
            version=self._next_version("credit_offer", offer_ref),
            payload={
                "offer_version": offer_version,
                "rule_id": rule_id,
                "rule_version": rule["rule_version"],
                "inputs_hash": canonical_hash(inputs),
                "decision": derived,
                "assessor_role": assessor_role,
            },
        )

    @staticmethod
    def _evaluate_rules(rules: Mapping[str, Any], inputs: Mapping[str, Any]) -> str:
        """规则引擎仅按已发布规则的阈值判断；没有规则覆盖时转人工复核。"""
        monthly_income = inputs.get("monthly_income")
        monthly_payment = inputs.get("monthly_payment")
        threshold = rules.get("max_payment_to_income")
        if monthly_income is None or monthly_payment is None or threshold is None:
            return "manual_review"
        ratio = D(monthly_payment) / D(monthly_income)
        if ratio > D(str(threshold)):
            return "ineligible"
        if inputs.get("existing_credit_institutions") and len(inputs["existing_credit_institutions"]) >= int(
            rules.get("max_institutions", 3)
        ):
            return "manual_review"
        return "eligible"

    # ---- 决定二：信贷同意 ------------------------------------------

    def capture_credit_consent(
        self,
        case_id: str,
        action_ref: str,
        offer_version: str,
        ui_hash: str,
        business_key: str,
        explicit: bool = True,
    ) -> StoredEvent:
        if explicit is not True:
            raise ConsentError("信贷同意必须是用户的显式动作，默认勾选或预填不能代替授权")
        state = self._state(case_id)
        self._guard_active(state)
        offer = state.offers.get(offer_version)
        if offer is None:
            raise ConsentError("同意必须针对已展示的具体要约版本")
        fingerprint = canonical_hash(
            ("credit_consent", action_ref, offer_version, ui_hash, offer.terms_hash)
        )
        prior = self._idempotency_check(case_id, business_key, fingerprint)
        if prior is not None:
            return prior
        if "payment_confirmation" not in state.consents:
            raise ConsentError("缺少独立的支付确认决定，支付确认不能被信贷同意吸收")
        if ui_hash != offer.ui_hash:
            self._freeze(
                case_id,
                "fingerprint_conflict",
                {"offer_ui_hash": offer.ui_hash, "consent_ui_hash": ui_hash},
            )
            raise FrozenCaseError("同意动作所在界面与要约展示界面指纹不一致，案件已冻结")
        if not state.offer_version_alive(offer_version):
            raise ConsentError("合同主体或费率已变更，该要约版本失效，必须对新要约重新确认")
        assessment = state.assessments.get(offer_version)
        if assessment is None:
            raise ConsentError("信贷同意前必须完成负担能力（适当性）评估")
        if assessment["decision"] != "eligible":
            raise ConsentError(f"适当性结论为 {assessment['decision']}，不得取得信贷同意")
        existing = state.consents.get("credit_agreement")
        if existing is not None and existing.offer_version == offer_version:
            raise ConsentError("该要约版本已取得显式同意，不得重复签约")
        record_id = f"consent-{case_id}-credit_agreement"
        stored = self._append(
            event_type="CONSENT_CAPTURED",
            aggregate_type="consent_record",
            aggregate_id=record_id,
            case_id=case_id,
            version=self._next_version("consent_record", record_id),
            payload={
                "consent_kind": "credit_agreement",
                "action_ref": action_ref,
                "offer_version": offer_version,
                "ui_hash": ui_hash,
                "terms_hash": offer.terms_hash,
                "explicit": True,
                "business_key": business_key,
                "request_fingerprint": fingerprint,
            },
            business_key=business_key,
        )
        if state.phase == PHASE_INITIATED:
            self._phase(self._state(case_id), PHASE_CAPTURED)
        return stored

    # ---- 签约、重确认、撤销 ---------------------------------------

    def sign_contract(
        self, case_id: str, contract_ref: str, parties: list[Mapping[str, Any]], business_key: str
    ) -> StoredEvent:
        state = self._state(case_id)
        self._guard_active(state)
        credit = state.consents.get("credit_agreement")
        if credit is None:
            raise ConsentError("签约前必须取得显式信贷同意")
        offer = state.offers[credit.offer_version]
        fingerprint = canonical_hash(
            ("sign_contract", contract_ref, offer.terms_hash, sorted(p.get("role", "") for p in parties))
        )
        prior = self._idempotency_check(case_id, business_key, fingerprint)
        if prior is not None:
            return prior
        if not state.offer_version_alive(credit.offer_version):
            raise ConsentError("要约条款已变更，必须重新取得同意后才能签约")
        if state.contract is not None:
            raise InvalidStateError("合同已签署，不得重复签约")
        cooling_off_until = (self.clock.now() + timedelta(days=COOLING_OFF_DAYS)).isoformat()
        with self.ledger.exclusive():
            stored = self._append(
                event_type="CONTRACT_SIGNED",
                aggregate_type="credit_contract",
                aggregate_id=contract_ref,
                case_id=case_id,
                version=self._next_version("credit_contract", contract_ref),
                payload={
                    "contract_ref": contract_ref,
                    "parties": [dict(p) for p in parties],
                    "offer_version": credit.offer_version,
                    "terms_hash": offer.terms_hash,
                    "principal": str(offer.principal),
                    "total_cost": str(offer.total_cost),
                    "cooling_off_until": cooling_off_until,
                    "business_key": business_key,
                    "request_fingerprint": fingerprint,
                },
                business_key=business_key,
            )
            self._phase(self._state(case_id), PHASE_COOLING_OFF)
        return stored

    def require_reconfirmation(
        self,
        case_id: str,
        reason: str,
        changed_fields: list[str],
        new_terms: Mapping[str, Any],
    ) -> StoredEvent:
        """合同主体或费率变化：旧条款上的同意即刻失效，等待新要约与新的显式同意。"""
        if reason not in {"terms_changed", "party_changed", "rate_changed"}:
            raise DomainError("重确认原因不合法")
        state = self._state(case_id)
        self._guard_active(state)
        if state.latest_offer_version is None:
            raise InvalidStateError("尚未展示要约，无需重确认")
        previous_hash = state.offers[state.latest_offer_version].terms_hash
        contract_ref = state.contract["contract_ref"] if state.contract else f"contract-{case_id}"
        return self._append(
            event_type="RECONFIRMATION_REQUIRED",
            aggregate_type="credit_contract",
            aggregate_id=contract_ref,
            case_id=case_id,
            version=self._next_version("credit_contract", contract_ref),
            payload={
                "reason": reason,
                "changed_fields": list(changed_fields),
                "previous_terms_hash": previous_hash,
                "new_terms_hash": canonical_hash(new_terms),
            },
        )

    def revoke_credit(self, case_id: str, reason: str = "user_request") -> StoredEvent:
        state = self._state(case_id)
        self._guard_active(state)
        if state.revoked is not None:
            raise InvalidStateError("授信已撤销")
        if state.disbursed:
            raise InvalidStateError("已实际放款（授信已使用），不能按未使用授信撤销，应走还款/争议流程")
        if state.contract is None:
            raise InvalidStateError("合同尚未签署，无授信可撤销")
        offer = state.offers[state.contract["offer_version"]]
        stored = self._append(
            event_type="CREDIT_REVOKED",
            aggregate_type="credit_contract",
            aggregate_id=state.contract["contract_ref"],
            case_id=case_id,
            version=self._next_version("credit_contract", state.contract["contract_ref"]),
            payload={
                "unused": True,
                "released_amount": str(offer.principal),
                "reason": reason,
            },
        )
        # 历史证据（展示、授权、评估、合同）全部保留，只推进阶段。
        self._phase(self._state(case_id), PHASE_REVOKED)
        return stored

    def _reconfirmation_pending(self, state: CaseState) -> str | None:
        """存在尚未被新显式同意承接的条款变更时，返回待确认的新条款哈希。"""
        if not state.reconfirmations:
            return None
        new_hash = state.reconfirmations[-1]["new_terms_hash"]
        credit = state.consents.get("credit_agreement")
        if credit is not None:
            offer = state.offers.get(credit.offer_version or "")
            if offer is not None and offer.terms_hash == new_hash:
                return None
        return new_hash

    # ---- 放款、费用、账单、扣款、还款 ------------------------------

    def disburse(
        self, case_id: str, amount: str, lender_ref: str, business_key: str
    ) -> StoredEvent:
        state = self._state(case_id)
        self._guard_active(state)
        if state.contract is None:
            raise InvalidStateError("未签约不得放款")
        fingerprint = canonical_hash(("disburse", amount, lender_ref, state.contract["contract_ref"]))
        prior = self._idempotency_check(case_id, business_key, fingerprint)
        if prior is not None:
            return prior
        if state.revoked is not None:
            raise InvalidStateError("授信已撤销，不得放款")
        if state.disbursed:
            raise InvalidStateError("已放款，不得重复放款")
        if self._reconfirmation_pending(state) is not None:
            raise InvalidStateError("合同主体或费率已变更，必须对新要约重新确认后才能放款")
        contract_amount = state.offers[state.contract["offer_version"]].principal
        if D(amount) != contract_amount:
            self._freeze(
                case_id,
                "amount_conflict",
                {"business_key": business_key, "contract_principal": str(contract_amount), "amount": amount},
            )
            raise FrozenCaseError("放款金额与合同本金不一致，案件已冻结")
        fund_id = f"fund-{case_id}"
        return self._append(
            event_type="FUNDS_DISBURSED",
            aggregate_type="fund_obligation",
            aggregate_id=fund_id,
            case_id=case_id,
            version=self._next_version("fund_obligation", fund_id),
            payload={
                "lender_ref": lender_ref,
                "amount": str(D(amount)),
                "contract_ref": state.contract["contract_ref"],
                "business_key": business_key,
                "request_fingerprint": fingerprint,
            },
            business_key=business_key,
        )

    def charge_fee(
        self, case_id: str, fee_type: str, amount: str, creditor_ref: str, business_key: str
    ) -> StoredEvent:
        state = self._state(case_id)
        self._guard_active(state)
        fingerprint = canonical_hash(("charge_fee", fee_type, amount, creditor_ref))
        prior = self._idempotency_check(case_id, business_key, fingerprint)
        if prior is not None:
            return prior
        fund_id = f"fund-{case_id}"
        return self._append(
            event_type="FEE_CHARGED",
            aggregate_type="fund_obligation",
            aggregate_id=fund_id,
            case_id=case_id,
            version=self._next_version("fund_obligation", fund_id),
            payload={
                "fee_type": fee_type,
                "amount": str(D(amount)),
                "creditor_ref": creditor_ref,
                "business_key": business_key,
                "request_fingerprint": fingerprint,
            },
            business_key=business_key,
        )

    def register_repayment_account(
        self, account_ref: str, available_amount: str, currency: str
    ) -> StoredEvent:
        return self._append(
            event_type="BALANCE_REGISTERED",
            aggregate_type="repayment_account",
            aggregate_id=account_ref,
            case_id="",
            version=self._next_version("repayment_account", account_ref),
            payload={"available_amount": str(D(available_amount)), "currency": currency},
        )

    def available_balance(self, account_ref: str) -> Decimal:
        affecting = [
            r.event
            for r in self.ledger.records()
            if r.event["aggregate_type"] == "repayment_account"
            and r.event["aggregate_id"] == account_ref
            or (
                r.event["event_type"] == "DEDUCTION_REQUESTED"
                and r.event["payload"].get("target_balance_ref") == account_ref
            )
        ]
        return project_balance(affecting)

    def request_deduction(
        self, case_id: str, request_ref: str, amount: str, target_balance_ref: str, business_key: str
    ) -> StoredEvent:
        """对同一可用余额的争用在账本临界区内原子裁决。"""
        state = self._state(case_id)
        self._guard_active(state)
        fingerprint = canonical_hash(("deduction", request_ref, amount, target_balance_ref))
        prior = self._idempotency_check(case_id, business_key, fingerprint)
        if prior is not None:
            return prior
        fund_id = f"fund-{case_id}"
        with self.ledger.exclusive():
            balance = self.available_balance(target_balance_ref)
            wanted = D(amount)
            outcome = "settled" if balance >= wanted else "insufficient_funds"
            stored = self._append(
                event_type="DEDUCTION_REQUESTED",
                aggregate_type="fund_obligation",
                aggregate_id=fund_id,
                case_id=case_id,
                version=self._next_version("fund_obligation", fund_id),
                payload={
                    "request_ref": request_ref,
                    "amount": str(wanted),
                    "target_balance_ref": target_balance_ref,
                    "outcome": outcome,
                    "business_key": business_key,
                    "request_fingerprint": fingerprint,
                    "balance_after": str(balance - wanted) if outcome == "settled" else str(balance),
                },
                business_key=business_key,
            )
        return stored

    def issue_bill(self, case_id: str, bill_ref: str, due_amount: str, due_date: str) -> StoredEvent:
        state = self._state(case_id)
        self._guard_active(state)
        fund_id = f"fund-{case_id}"
        stored = self._append(
            event_type="REPAYMENT_DUE",
            aggregate_type="fund_obligation",
            aggregate_id=fund_id,
            case_id=case_id,
            version=self._next_version("fund_obligation", fund_id),
            payload={"bill_ref": bill_ref, "due_amount": str(D(due_amount)), "due_date": due_date},
        )
        if state.phase == PHASE_ACTIVE:
            self._phase(self._state(case_id), PHASE_BILLING)
        return stored

    def repay(self, case_id: str, bill_ref: str, amount: str, business_key: str) -> StoredEvent:
        state = self._state(case_id)
        self._guard_active(state)
        fingerprint = canonical_hash(("repay", bill_ref, amount))
        prior = self._idempotency_check(case_id, business_key, fingerprint)
        if prior is not None:
            return prior
        fund_id = f"fund-{case_id}"
        stored = self._append(
            event_type="REPAYMENT_MADE",
            aggregate_type="fund_obligation",
            aggregate_id=fund_id,
            case_id=case_id,
            version=self._next_version("fund_obligation", fund_id),
            payload={
                "bill_ref": bill_ref,
                "amount": str(D(amount)),
                "business_key": business_key,
                "request_fingerprint": fingerprint,
            },
            business_key=business_key,
        )
        bills_total = sum((D(b["due_amount"]) for b in state.bills), Decimal("0"))
        paid_total = sum((D(r["amount"]) for r in state.repayments), Decimal("0")) + D(amount)
        if paid_total >= bills_total:
            self._phase(self._state(case_id), PHASE_SETTLED)
        return stored

    # ---- 通知与异议 -----------------------------------------------

    def send_notice(
        self, case_id: str, notice_kind: str, channel: str, template_ref: str, recipient_ref: str
    ) -> StoredEvent:
        state = self._state(case_id)
        return self._append(
            event_type="NOTICE_SENT",
            aggregate_type="case_file",
            aggregate_id=case_id,
            case_id=case_id,
            version=self._next_version("case_file", case_id),
            payload={
                "notice_kind": notice_kind,
                "channel": channel,
                "template_ref": template_ref,
                "recipient_ref": recipient_ref,
            },
        )

    def open_dispute(self, case_id: str, reason: str, raised_by_ref: str) -> StoredEvent:
        state = self._state(case_id)
        if state.bills:
            latest_due = datetime.fromisoformat(state.bills[-1]["due_date"])
            deadline = latest_due + timedelta(days=DISPUTE_WINDOW_DAYS)
            if self.clock.now() > deadline:
                raise DomainError(
                    f"异议期限已于 {deadline.isoformat()} 届满；逾期异议须走单独的申诉登记"
                )
        stored = self._append(
            event_type="DISPUTE_OPENED",
            aggregate_type="case_file",
            aggregate_id=case_id,
            case_id=case_id,
            version=self._next_version("case_file", case_id),
            payload={"reason": reason, "raised_by_ref": raised_by_ref},
        )
        if not state.is_frozen:
            self._phase(self._state(case_id), PHASE_DISPUTE)
        return stored

    def resolve_dispute(self, case_id: str, resolution: str, resolver_ref: str) -> StoredEvent:
        state = self._state(case_id)
        stored = self._append(
            event_type="DISPUTE_RESOLVED",
            aggregate_type="case_file",
            aggregate_id=case_id,
            case_id=case_id,
            version=self._next_version("case_file", case_id),
            payload={"resolution": resolution, "resolver_ref": resolver_ref},
        )
        if state.phase == PHASE_DISPUTE:
            overdue = bool(
                state.bills
                and self.clock.now() > datetime.fromisoformat(state.bills[-1]["due_date"])
            )
            target = PHASE_OVERDUE if overdue else (PHASE_BILLING if state.bills else PHASE_ACTIVE)
            self._phase(self._state(case_id), target)
        return stored

    # ---- 时钟驱动的阶段推进 ---------------------------------------

    def evaluate_time_progression(self, case_id: str) -> list[StoredEvent]:
        """按可控时钟推进犹豫期与逾期；重启后仅依据事件历史判断，阶段不会重置。"""
        state = self._state(case_id)
        advanced: list[StoredEvent] = []
        now = self.clock.now()
        if state.phase == PHASE_COOLING_OFF and state.contract is not None:
            until = datetime.fromisoformat(state.contract["cooling_off_until"])
            if now >= until:
                advanced.append(self._phase(state, PHASE_ACTIVE))
                state = self._state(case_id)
        if state.phase == PHASE_BILLING and state.bills:
            due = datetime.fromisoformat(state.bills[-1]["due_date"])
            if now > due:
                advanced.append(self._phase(state, PHASE_OVERDUE))
                state = self._state(case_id)
                notice_kinds = {n["notice_kind"] for n in state.notices}
                if "overdue" not in notice_kinds:
                    advanced.append(
                        self.send_notice(case_id, "overdue", "sms", "tpl-overdue-v1", "user")
                    )
        if state.phase == PHASE_OVERDUE and state.bills:
            due = datetime.fromisoformat(state.bills[-1]["due_date"])
            if now > due + timedelta(days=DISPUTE_WINDOW_DAYS):
                notice_kinds = {n["notice_kind"] for n in self._state(case_id).notices}
                if "collection" not in notice_kinds:
                    advanced.append(
                        self.send_notice(case_id, "collection", "sms", "tpl-collection-v1", "user")
                    )
        return advanced
