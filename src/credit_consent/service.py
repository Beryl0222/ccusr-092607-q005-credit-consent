"""穿透账应用服务：所有金融消费者保护不变量的强制执行点。

关键规则：
1. 支付确认与信贷同意是两个独立的显式决定；默认勾选、立减优惠都不能代替授权；
2. 营销配置人员只能起草适当性规则，批准权在合规；评估也不得由营销角色完成；
3. 同一业务键出现不同界面指纹或金额 → 立即冻结；冻结期间禁止一切资金动作；
4. 放款/扣款按幂等键重放，网络重试不重复签约、放款、扣款；
5. 扣款争用同一可用余额时在独占事务内原子裁决，不足即留拒绝记录；
6. 撤销尚未使用的授信：释放额度、保留全部历史证据；已动用则不得撤销；
7. 合同主体或费率变化必须重新确认，否则禁止放款与扣款；
8. 犹豫期、账单到期、逾期、异议窗由可控时钟推进，重启后阶段不重置。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any, Optional

from .clock import ControllableClock
from .ledger import Ledger
from .model import BusinessState, money

ROLE_MARKETING = "marketing"
ROLE_COMPLIANCE = "compliance"
ROLE_LENDER = "lender"
ROLE_COLLECTOR = "collector"
ROLE_PAYER = "payer"

PAYMENT_CONFIRMATION = "payment_confirmation"
CREDIT_CONSENT = "credit_consent"


class DomainError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class TimelinePolicy:
    cooling_off_days: int = 1
    overdue_grace_days: int = 0
    dispute_window_days: int = 30


class CreditConsentService:
    def __init__(self, ledger: Ledger, policy: Optional[TimelinePolicy] = None) -> None:
        self.ledger = ledger
        self.policy = policy or TimelinePolicy()

    # ---- 内部工具 ----

    def _state(self, business_key: str) -> BusinessState:
        return BusinessState.from_events(business_key, self.ledger.events_for(business_key))

    def _guard_mutable(self, state: BusinessState) -> None:
        if state.frozen:
            raise DomainError("business_frozen", f"业务键已冻结：{state.freeze_reason}")

    def _freeze(self, business_key: str, reason: str, evidence: dict[str, Any]) -> None:
        """冻结事件本身必须落链（调用方已持锁）。"""
        self.ledger.append_event(
            "FREEZE_RAISED",
            "compliance_freeze",
            f"cf-{business_key}",
            {"business_key": business_key, "reason": reason, "evidence": evidence},
        )

    def _ids(self, bk: str) -> dict[str, str]:
        return {
            "intent": f"pi-{bk}",
            "offer": f"of-{bk}",
            "assessment": f"sa-{bk}",
            "consent": f"cr-{bk}",
            "line": f"cl-{bk}",
            "agreement": f"ca-{bk}",
            "obligation": f"fo-{bk}",
        }

    # ---- 界面与适当性治理 ----

    def publish_ui_version(self, ui_hash: str, content_hash: str) -> dict[str, Any]:
        with self.ledger.exclusive():
            return self.ledger.append_event(
                "UI_VERSION_PUBLISHED",
                "credit_offer",
                f"ui-{ui_hash}",
                {"ui_hash": ui_hash, "content_hash": content_hash},
            )

    def draft_ruleset(self, ruleset_version: str, actor_role: str) -> dict[str, Any]:
        with self.ledger.exclusive():
            return self.ledger.append_event(
                "RULESET_DRAFTED",
                "suitability_ruleset",
                f"rs-{ruleset_version}",
                {"ruleset_version": ruleset_version, "drafted_by_role": actor_role},
            )

    def approve_ruleset(self, ruleset_version: str, actor_role: str) -> dict[str, Any]:
        """营销配置人员无权批准适当性规则；只有合规角色可以。"""
        if actor_role == ROLE_MARKETING:
            raise DomainError("forbidden_approver", "营销配置人员无权批准适当性规则")
        drafts = [
            e for e in self.ledger.all_events()
            if e["event_type"] == "RULESET_DRAFTED" and e["payload"]["ruleset_version"] == ruleset_version
        ]
        if not drafts:
            raise DomainError("ruleset_not_drafted", "规则集尚未起草，不能批准")
        with self.ledger.exclusive():
            return self.ledger.append_event(
                "RULESET_APPROVED",
                "suitability_ruleset",
                f"rs-{ruleset_version}",
                {"ruleset_version": ruleset_version, "approved_by_role": actor_role},
            )

    def _ruleset_is_approved(self, ruleset_version: str) -> bool:
        return any(
            e["event_type"] == "RULESET_APPROVED" and e["payload"]["ruleset_version"] == ruleset_version
            for e in self.ledger.all_events()
        )

    # ---- 意图与展示 ----

    def create_intent(
        self,
        business_key: str,
        merchant_ref: str,
        amount: str,
        currency: str,
        incentive_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        with self.ledger.exclusive():
            if self.ledger.events_for(business_key):
                raise DomainError("business_key_exists", "业务键已存在，不得重复创建意图")
            return self.ledger.append_event(
                "INTENT_CREATED",
                "payment_intent",
                self._ids(business_key)["intent"],
                {
                    "business_key": business_key,
                    "merchant_ref": merchant_ref,
                    "amount": str(money(amount)),
                    "currency": currency,
                    "incentive_snapshot": incentive_snapshot,
                },
            )

    def present_offer(
        self,
        business_key: str,
        ui_hash: str,
        offer_version: str,
        lender_ref: str,
        principal: str,
        total_cost: str,
        apr: str,
        fee_schedule: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """展示要约。同一业务键上界面指纹或金额与既有事实不一致即冻结。"""
        with self.ledger.exclusive():
            state = self._state(business_key)
            self._guard_mutable(state)
            if state.intent is None:
                raise DomainError("intent_missing", "支付意图不存在，不能展示信贷要约")
            principal_d = money(principal)
            evidence: dict[str, Any] = {}
            if state.intent["amount"] != str(principal_d):
                evidence = {
                    "field": "amount",
                    "intent_amount": state.intent["amount"],
                    "offer_principal": str(principal_d),
                }
            for prior in state.offers:
                if prior["ui_hash"] != ui_hash or money(prior["principal"]) != principal_d:
                    evidence = {
                        "field": "ui_hash_or_amount",
                        "previous_ui_hash": prior["ui_hash"],
                        "incoming_ui_hash": ui_hash,
                        "previous_principal": prior["principal"],
                        "incoming_principal": str(principal_d),
                    }
                    break
            if state.payment_confirmed is not None and state.payment_confirmed["ui_hash"] != ui_hash:
                evidence = {
                    "field": "ui_hash",
                    "payment_ui_hash": state.payment_confirmed["ui_hash"],
                    "incoming_ui_hash": ui_hash,
                }
            if evidence:
                self._freeze(business_key, "同一业务键出现不同界面指纹或金额", evidence)
                raise DomainError("business_frozen", "界面指纹或金额冲突，业务键已立即冻结")
            return self.ledger.append_event(
                "OFFER_PRESENTED",
                "credit_offer",
                self._ids(business_key)["offer"],
                {
                    "business_key": business_key,
                    "ui_hash": ui_hash,
                    "offer_version": offer_version,
                    "lender_ref": lender_ref,
                    "principal": str(principal_d),
                    "total_cost": str(money(total_cost)),
                    "apr": apr,
                    "fee_schedule": fee_schedule,
                },
            )

    # ---- 两个独立决定 ----

    def confirm_payment(self, business_key: str, action_ref: str, ui_hash: str) -> dict[str, Any]:
        """第一个决定：支付确认。它不产生任何信贷同意。"""
        with self.ledger.exclusive():
            state = self._state(business_key)
            self._guard_mutable(state)
            if state.intent is None:
                raise DomainError("intent_missing", "支付意图不存在")
            if state.payment_confirmed is not None:
                raise DomainError("payment_already_confirmed", "支付确认只能做出一次")
            if state.current_offer is None:
                raise DomainError("offer_missing", "尚未展示要约，无法确认支付")
            if state.current_offer["ui_hash"] != ui_hash:
                self._freeze(
                    business_key,
                    "支付确认界面指纹与展示记录不一致",
                    {"offer_ui_hash": state.current_offer["ui_hash"], "confirmed_ui_hash": ui_hash},
                )
                raise DomainError("business_frozen", "界面指纹冲突，业务键已冻结")
            return self.ledger.append_event(
                "PAYMENT_CONFIRMED",
                "payment_intent",
                self._ids(business_key)["intent"],
                {"business_key": business_key, "action_ref": action_ref, "ui_hash": ui_hash},
            )

    def evaluate_suitability(
        self,
        business_key: str,
        ruleset_version: str,
        input_fields: dict[str, Any],
        inputs_hash: str,
        decision: str,
        actor_role: str,
    ) -> dict[str, Any]:
        """适当性评估：规则集必须已由合规批准，营销角色既不能批准规则也不能执行评估。"""
        if actor_role == ROLE_MARKETING:
            raise DomainError("forbidden_evaluator", "营销配置人员无权执行适当性评估")
        if not self._ruleset_is_approved(ruleset_version):
            raise DomainError("ruleset_not_approved", "适当性规则集未经合规批准，不得用于评估")
        if decision not in ("approved", "rejected", "manual_review"):
            raise DomainError("bad_decision", "评估结论必须是 approved/rejected/manual_review")
        with self.ledger.exclusive():
            state = self._state(business_key)
            self._guard_mutable(state)
            if state.current_offer is None:
                raise DomainError("offer_missing", "尚未展示要约，无法评估")
            return self.ledger.append_event(
                "SUITABILITY_EVALUATED",
                "suitability_assessment",
                self._ids(business_key)["assessment"],
                {
                    "business_key": business_key,
                    "ruleset_version": ruleset_version,
                    "inputs_hash": inputs_hash,
                    "input_fields": input_fields,
                    "decision": decision,
                    "evaluated_by_role": actor_role,
                },
            )

    def capture_credit_consent(self, business_key: str, action_ref: str) -> dict[str, Any]:
        """第二个决定：信贷同意。必须显式、独立，且与支付确认是两次不同动作。"""
        with self.ledger.exclusive():
            state = self._state(business_key)
            self._guard_mutable(state)
            offer = state.current_offer
            if offer is None:
                raise DomainError("offer_missing", "没有可同意的信贷要约")
            if state.payment_confirmed is None:
                raise DomainError("payment_not_confirmed", "必须先完成支付确认，才能单独做出信贷同意")
            if action_ref == state.payment_confirmed["action_ref"]:
                raise DomainError(
                    "single_action_for_two_decisions",
                    "支付确认与信贷同意必须是两个明确决定，不得复用同一次动作",
                )
            if state.suitability is None or state.suitability["decision"] != "approved":
                raise DomainError("suitability_not_approved", "适当性评估未通过，不得采集信贷同意")
            if state.has_explicit_consent(CREDIT_CONSENT):
                raise DomainError("credit_consent_exists", "信贷同意只能做出一次；变更必须重新确认")
            return self.ledger.append_event(
                "CONSENT_CAPTURED",
                "consent_record",
                self._ids(business_key)["consent"],
                {
                    "business_key": business_key,
                    "action_ref": action_ref,
                    "offer_version": offer["offer_version"],
                    "consent_kind": CREDIT_CONSENT,
                    "ui_hash": offer["ui_hash"],
                },
            )

    # ---- 撤销：释放额度但保留证据 ----

    def withdraw_credit(self, business_key: str, reason: str) -> list[dict[str, Any]]:
        with self.ledger.exclusive():
            state = self._state(business_key)
            self._guard_mutable(state)
            if not state.has_explicit_consent(CREDIT_CONSENT):
                raise DomainError("consent_missing", "不存在有效的信贷同意，无需撤销")
            if state.disbursed > 0:
                raise DomainError("credit_already_used", "授信已实际动用，不能按未使用授信撤销")
            ids = self._ids(business_key)
            specs = [
                (
                    "CONSENT_WITHDRAWN",
                    "consent_record",
                    ids["consent"],
                    {"business_key": business_key, "reason": reason},
                )
            ]
            if state.line is not None:
                releasable = money(state.line["limit"]) - state.released
                specs.append(
                    (
                        "CREDIT_LIMIT_RELEASED",
                        "credit_line",
                        ids["line"],
                        {
                            "business_key": business_key,
                            "amount": str(releasable),
                            "currency": state.line["currency"],
                            "retain_evidence": True,
                        },
                    )
                )
            return self.ledger.commit_batch(specs)

    # ---- 授信、合同与条款变更 ----

    def open_credit_line(self, business_key: str, limit: str, currency: str) -> dict[str, Any]:
        with self.ledger.exclusive():
            state = self._state(business_key)
            self._guard_mutable(state)
            if not state.has_explicit_consent(CREDIT_CONSENT):
                raise DomainError("consent_missing", "缺少信贷同意，不能开立授信")
            if state.line is not None:
                raise DomainError("line_exists", "授信已开立")
            offer = state.current_offer
            return self.ledger.append_event(
                "CREDIT_LINE_OPENED",
                "credit_line",
                self._ids(business_key)["line"],
                {
                    "business_key": business_key,
                    "limit": str(money(limit)),
                    "currency": currency,
                    "offer_version": offer["offer_version"],
                },
            )

    def sign_agreement(self, business_key: str, parties: list[dict[str, str]]) -> dict[str, Any]:
        with self.ledger.exclusive():
            state = self._state(business_key)
            self._guard_mutable(state)
            if not state.has_explicit_consent(PAYMENT_CONFIRMATION):
                raise DomainError("payment_not_confirmed", "缺少支付确认")
            if not state.has_explicit_consent(CREDIT_CONSENT):
                raise DomainError("consent_missing", "缺少信贷同意")
            if state.suitability is None or state.suitability["decision"] != "approved":
                raise DomainError("suitability_not_approved", "适当性评估未通过，不能签署合同")
            if not any(p.get("role") == ROLE_LENDER for p in parties):
                raise DomainError("lender_missing", "合同参与方必须包含放款方")
            if state.agreement is not None:
                raise DomainError("agreement_exists", "合同已签署；条款变化必须走重新确认")
            offer = state.current_offer
            return self.ledger.append_event(
                "AGREEMENT_SIGNED",
                "credit_agreement",
                self._ids(business_key)["agreement"],
                {
                    "business_key": business_key,
                    "offer_version": offer["offer_version"],
                    "parties": parties,
                    "signed_at": self.ledger.clock.iso(),
                },
            )

    def change_terms(
        self, business_key: str, new_offer_version: str, changed_fields: list[str]
    ) -> dict[str, Any]:
        """合同主体或费率变化：挂起合同，等待重新确认，期间禁止放款、扣款。"""
        with self.ledger.exclusive():
            state = self._state(business_key)
            self._guard_mutable(state)
            if state.agreement is None:
                raise DomainError("agreement_missing", "合同尚未签署，无所谓变更")
            return self.ledger.append_event(
                "AGREEMENT_TERMS_CHANGED",
                "credit_agreement",
                self._ids(business_key)["agreement"],
                {
                    "business_key": business_key,
                    "previous_offer_version": state.agreement["offer_version"],
                    "new_offer_version": new_offer_version,
                    "changed_fields": changed_fields,
                },
            )

    def reconfirm_agreement(self, business_key: str, new_offer_version: str, action_ref: str) -> dict[str, Any]:
        with self.ledger.exclusive():
            state = self._state(business_key)
            self._guard_mutable(state)
            if not state.pending_reconfirmation:
                raise DomainError("no_pending_change", "没有待确认的条款变更")
            existing = {c.action_ref for c in state.consents.values()}
            if action_ref in existing:
                raise DomainError("reused_action", "重新确认必须是一次新的显式动作")
            return self.ledger.append_event(
                "AGREEMENT_RECONFIRMED",
                "credit_agreement",
                self._ids(business_key)["agreement"],
                {
                    "business_key": business_key,
                    "new_offer_version": new_offer_version,
                    "action_ref": action_ref,
                },
            )

    # ---- 放款与收费（幂等）----

    def disburse(
        self,
        business_key: str,
        idempotency_key: str,
        amount: str,
        currency: str,
    ) -> dict[str, Any]:
        with self.ledger.exclusive():
            prior = self.ledger.find_idempotent(business_key, "FUNDS_DISBURSED", idempotency_key)
            if prior is not None:
                return prior  # 网络重试：原样返回，不重复放款
            state = self._state(business_key)
            self._guard_mutable(state)
            if state.agreement is None:
                raise DomainError("agreement_missing", "合同未签署，不能放款")
            if state.pending_reconfirmation:
                raise DomainError("terms_changed", "条款已变更且未重新确认，禁止放款")
            if state.disbursed > 0:
                raise DomainError("already_disbursed", "该业务已放款；重试必须复用原幂等键")
            if state.withdrawn:
                raise DomainError("credit_withdrawn", "授信已被撤销，不能放款")
            amount_d = money(amount)
            if amount_d > state.credit_available:
                raise DomainError("limit_exceeded", "放款金额超过可用额度")
            offer = state.current_offer
            return self.ledger.append_event(
                "FUNDS_DISBURSED",
                "fund_obligation",
                self._ids(business_key)["obligation"],
                {
                    "business_key": business_key,
                    "idempotency_key": idempotency_key,
                    "lender_ref": offer["lender_ref"],
                    "amount": str(amount_d),
                    "currency": currency,
                    "offer_version": offer["offer_version"],
                },
            )

    def charge_fee(
        self, business_key: str, fee_type: str, amount: str, currency: str, party_ref: str
    ) -> dict[str, Any]:
        with self.ledger.exclusive():
            state = self._state(business_key)
            self._guard_mutable(state)
            return self.ledger.append_event(
                "FEE_CHARGED",
                "fund_obligation",
                self._ids(business_key)["obligation"],
                {
                    "business_key": business_key,
                    "fee_type": fee_type,
                    "amount": str(money(amount)),
                    "currency": currency,
                    "party_ref": party_ref,
                },
            )

    # ---- 账单与原子扣款 ----

    def issue_bill(
        self,
        business_key: str,
        amount_due: str,
        currency: str,
        period_start: str,
        period_end: str,
        due_at: str,
        available_balance: str,
    ) -> dict[str, Any]:
        with self.ledger.exclusive():
            state = self._state(business_key)
            self._guard_mutable(state)
            seq = max(state.bills, default=0) + 1
            return self.ledger.append_event(
                "BILL_ISSUED",
                "repayment_bill",
                f"rb-{business_key}-{seq}",
                {
                    "business_key": business_key,
                    "bill_seq": seq,
                    "amount_due": str(money(amount_due)),
                    "currency": currency,
                    "period_start": period_start,
                    "period_end": period_end,
                    "due_at": due_at,
                    "available_balance": str(money(available_balance)),
                },
            )

    def _channel_available(self, state: BusinessState) -> Decimal:
        """该业务键当前可用扣款余额：最近账单登记的余额，减去全部已结清扣款。

        多个扣款请求争用同一余额时，调用方持有独占事务锁，
        因此这里的"读余额—裁决—落账"是原子的。
        """
        registered = Decimal("0")
        for event in reversed(state.events):
            if event["event_type"] == "BILL_ISSUED":
                registered = money(event["payload"]["available_balance"])
                break
        settled = sum(
            (money(e["payload"]["amount"]) for e in state.events if e["event_type"] == "DEBIT_SETTLED"),
            Decimal("0"),
        )
        return registered - settled

    def request_debit(
        self,
        business_key: str,
        idempotency_key: str,
        bill_seq: int,
        amount: str,
        actor_role: str,
    ) -> dict[str, Any]:
        """扣款请求与裁决在同一独占事务完成：争用同一余额的并发请求被串行原子处理。"""
        with self.ledger.exclusive():
            prior = self.ledger.find_idempotent(business_key, "DEBIT_SETTLED", idempotency_key)
            if prior is not None:
                return prior
            prior_rejected = self.ledger.find_idempotent(business_key, "DEBIT_REJECTED", idempotency_key)
            if prior_rejected is not None:
                return prior_rejected
            state = self._state(business_key)
            self._guard_mutable(state)
            if bill_seq not in state.bills:
                raise DomainError("bill_missing", "账单不存在")
            if state.pending_reconfirmation:
                raise DomainError("terms_changed", "条款变更未重新确认，禁止扣款")
            bill = state.bills[bill_seq]
            amount_d = money(amount)
            if amount_d > bill.outstanding:
                verdict_event = "DEBIT_REJECTED"
                reason = "扣款金额超过账单未结余额"
            elif amount_d > self._channel_available(state):
                verdict_event = "DEBIT_REJECTED"
                reason = "可用余额不足，扣款请求被原子拒绝"
            else:
                verdict_event = "DEBIT_SETTLED"
                reason = ""
            verdict_payload: dict[str, Any] = {
                "business_key": business_key,
                "idempotency_key": idempotency_key,
                "bill_seq": bill_seq,
                "amount": str(amount_d),
                "currency": bill.currency,
            }
            if verdict_event == "DEBIT_REJECTED":
                verdict_payload["reason"] = reason
            specs = [
                (
                    "DEBIT_REQUESTED",
                    "repayment_bill",
                    f"rb-{business_key}-{bill_seq}",
                    {
                        "business_key": business_key,
                        "idempotency_key": idempotency_key,
                        "bill_seq": bill_seq,
                        "amount": str(amount_d),
                        "currency": bill.currency,
                        "requested_by_role": actor_role,
                    },
                ),
                (
                    verdict_event,
                    "repayment_bill",
                    f"rb-{business_key}-{bill_seq}",
                    verdict_payload,
                ),
            ]
            return self.ledger.commit_batch(specs)[-1]

    # ---- 时钟推进：犹豫期 / 账单 / 逾期 / 异议窗 ----

    def advance_clock(self, **kwargs: Any) -> list[dict[str, Any]]:
        """推进可控时钟，并为所有已跨越的阶段补发一次性阶段事件；随后持久化时钟检查点。"""
        delta = timedelta(**kwargs)
        with self.ledger.exclusive():
            self.ledger.clock.advance(delta)
            now = self.ledger.clock.now
            staged: list[tuple[str, str, str, dict[str, Any]]] = []
            for business_key in {e["payload"].get("business_key") for e in self.ledger.all_events()}:
                if not business_key:
                    continue
                state = self._state(business_key)
                if state.frozen:
                    continue
                ids = self._ids(business_key)
                # 犹豫期：自开立授信（无授信则自信贷同意）起算。
                anchor = None
                if state.line is not None:
                    line_events = [e for e in state.events if e["event_type"] == "CREDIT_LINE_OPENED"]
                    anchor = line_events[0] if line_events else None
                elif state.consents.get(CREDIT_CONSENT) is not None:
                    anchor = next(
                        (e for e in state.events if e["event_type"] == "CONSENT_CAPTURED"
                         and e["payload"]["consent_kind"] == CREDIT_CONSENT),
                        None,
                    )
                if anchor is not None and not state.cooling_off_expired and not state.withdrawn:
                    from .model import parse_ts

                    deadline = parse_ts(anchor["occurred_at"]) + timedelta(days=self.policy.cooling_off_days)
                    if now >= deadline:
                        staged.append(
                            ("COOLING_OFF_EXPIRED", "credit_line", ids["line"] or ids["consent"],
                             {"business_key": business_key})
                        )
                for seq, bill in sorted(state.bills.items()):
                    if now >= bill.due_at and not bill.due_noticed:
                        staged.append(
                            ("BILL_DUE", "repayment_bill", f"rb-{business_key}-{seq}",
                             {"business_key": business_key, "bill_seq": seq})
                        )
                    overdue_at = bill.due_at + timedelta(days=self.policy.overdue_grace_days)
                    if now >= overdue_at and bill.outstanding > 0 and not bill.overdue_noticed:
                        overdue_days = max((now.date() - bill.due_at.date()).days, 0)
                        staged.append(
                            ("ACCOUNT_OVERDUE", "repayment_bill", f"rb-{business_key}-{seq}",
                             {"business_key": business_key, "bill_seq": seq, "overdue_days": overdue_days})
                        )
                    window_end = bill.due_at + timedelta(days=self.policy.dispute_window_days)
                    if now >= window_end and bill.outstanding > 0 and not bill.window_closed:
                        staged.append(
                            ("DISPUTE_WINDOW_CLOSED", "repayment_bill", f"rb-{business_key}-{seq}",
                             {"business_key": business_key, "bill_seq": seq})
                        )
            emitted = self.ledger.commit_batch(staged) if staged else []
            self.ledger.checkpoint_clock()
            return emitted

    # ---- 催收与异议 ----

    def open_collection_case(self, business_key: str, collector_ref: str) -> dict[str, Any]:
        with self.ledger.exclusive():
            state = self._state(business_key)
            self._guard_mutable(state)
            overdue_bills = [b for b in state.bills.values() if b.overdue_noticed and b.outstanding > 0]
            if not overdue_bills:
                raise DomainError("not_overdue", "没有逾期未结账单，不能进入催收")
            outstanding = sum((b.outstanding for b in overdue_bills), Decimal("0"))
            seq = overdue_bills[0].seq
            currency = overdue_bills[0].currency
            return self.ledger.append_event(
                "COLLECTION_CASE_OPENED",
                "collection_case",
                f"cc-{business_key}",
                {
                    "business_key": business_key,
                    "bill_seq": seq,
                    "collector_ref": collector_ref,
                    "amount_outstanding": str(outstanding),
                    "currency": currency,
                },
            )

    def send_collection_notice(
        self, business_key: str, channel: str, template_version: str
    ) -> dict[str, Any]:
        with self.ledger.exclusive():
            state = self._state(business_key)
            self._guard_mutable(state)
            if state.collection_case is None:
                raise DomainError("case_missing", "催收案件不存在")
            notice_seq = max((n["notice_seq"] for n in state.notices), default=0) + 1
            return self.ledger.append_event(
                "COLLECTION_NOTICE_SENT",
                "collection_case",
                f"cc-{business_key}",
                {
                    "business_key": business_key,
                    "case_ref": f"cc-{business_key}",
                    "channel": channel,
                    "template_version": template_version,
                    "notice_seq": notice_seq,
                },
            )

    def open_dispute(self, business_key: str, reason: str, evidence_refs: list[str]) -> dict[str, Any]:
        with self.ledger.exclusive():
            state = self._state(business_key)
            if any("resolution" not in d for d in state.disputes):
                raise DomainError("dispute_open", "已有未结案的异议")
            closed = [b for b in state.bills.values() if b.window_closed]
            if closed:
                raise DomainError("window_closed", "异议期限已届满，本渠道不再受理（可走线下申诉留痕）")
            return self.ledger.append_event(
                "DISPUTE_OPENED",
                "dispute_case",
                f"dc-{business_key}",
                {
                    "business_key": business_key,
                    "reason": reason,
                    "evidence_refs": evidence_refs,
                },
            )

    def resolve_dispute(self, business_key: str, resolution: str) -> dict[str, Any]:
        with self.ledger.exclusive():
            state = self._state(business_key)
            if not any("resolution" not in d for d in state.disputes):
                raise DomainError("dispute_missing", "没有待处理的异议")
            return self.ledger.append_event(
                "DISPUTE_RESOLVED",
                "dispute_case",
                f"dc-{business_key}",
                {
                    "business_key": business_key,
                    "resolution": resolution,
                    "resolved_at": self.ledger.clock.iso(),
                },
            )

    # ---- 冻结处置 ----

    def lift_freeze(self, business_key: str, reason: str, actor_role: str) -> dict[str, Any]:
        if actor_role != ROLE_COMPLIANCE:
            raise DomainError("forbidden", "只有合规角色可以解除冻结")
        with self.ledger.exclusive():
            state = self._state(business_key)
            if not state.frozen:
                raise DomainError("not_frozen", "业务键当前未冻结")
            return self.ledger.append_event(
                "FREEZE_LIFTED",
                "compliance_freeze",
                f"cf-{business_key}",
                {"business_key": business_key, "reason": reason},
            )
