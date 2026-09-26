"""只读投影：同一条不可覆盖事件链，面向不同角色给出不同最小视图。

- 消费者视图：用可理解的话回答"这笔消费为何形成债务、真实成本、谁负责"；
- 审计视图：按历史时间原样还原展示、授权、评估、资金与催收全过程；
- 放款方视图：仅履责所需数据，且只能看到自己作为放款方的业务；
- 催收方视图：逾期金额、案件与已发通知，看不到适当性原始输入等无关数据。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from .model import BusinessState, money


class AccessDenied(Exception):
    """角色无权查看该业务键或该字段。"""


def _fee_total(state: BusinessState) -> Decimal:
    return sum((money(f["amount"]) for f in state.fees), Decimal("0"))


def consumer_explanation(state: BusinessState) -> dict[str, Any]:
    """给金融消费者的债务解释。"""
    offer = state.current_offer
    payment = state.payment_confirmed
    credit_consent = state.consents.get("credit_consent")
    explanation: dict[str, Any] = {
        "business_key": state.business_key,
        "这笔钱是怎么来的": None,
        "你做过的两次决定": [],
        "真实成本": None,
        "责任方": [],
        "当前状态": None,
    }
    if state.intent:
        incentive = state.intent.get("incentive_snapshot") or {}
        explanation["这笔钱是怎么来的"] = (
            f"你于 {state.events[0]['occurred_at']} 发起 {state.intent['amount']} "
            f"{state.intent['currency']} 的支付"
            + (f"，领取了优惠「{incentive.get('description', '')}」" if incentive else "")
            + "。支付本身不等于借钱。"
        )
    if payment:
        explanation["你做过的两次决定"].append(
            {"决定": "支付确认", "动作": payment["action_ref"], "界面指纹": payment["ui_hash"]}
        )
    if credit_consent and not credit_consent.withdrawn:
        explanation["你做过的两次决定"].append(
            {
                "决定": "信贷同意（独立的第二次决定）",
                "动作": credit_consent.action_ref,
                "要约版本": credit_consent.offer_version,
                "界面指纹": credit_consent.ui_hash,
            }
        )
    elif state.withdrawn:
        explanation["你做过的两次决定"].append({"决定": "信贷同意已撤销，未使用额度已释放"})
    if offer:
        explanation["真实成本"] = {
            "本金": f"{offer['principal']} {_currency_of(state)}",
            "综合融资成本总额": f"{offer['total_cost']} {_currency_of(state)}",
            "年化利率_APR": offer["apr"],
            "费用明细": offer.get("fee_schedule", []),
            "已计入费用": str(_fee_total(state)),
        }
        explanation["责任方"].append({"角色": "放款方", "标识": offer["lender_ref"]})
    if state.agreement:
        explanation["责任方"].extend(
            {"角色": p.get("role"), "标识": p.get("ref"), "名称": p.get("name")}
            for p in state.agreement.get("parties", [])
        )
    status_bits: list[str] = []
    if state.frozen:
        status_bits.append(f"已冻结（{state.freeze_reason}），资金动作暂停")
    if state.pending_reconfirmation:
        status_bits.append("合同主体或费率已变更，等待你重新确认，期间不会放款或扣款")
    if state.disbursed:
        status_bits.append(f"已实际放款 {state.disbursed}")
    outstanding = sum((b.outstanding for b in state.bills.values()), Decimal("0"))
    if outstanding:
        status_bits.append(f"当前待还 {outstanding}")
    if state.collection_case:
        status_bits.append(f"已进入催收（{state.collection_case['collector_ref']}）")
    explanation["当前状态"] = "；".join(status_bits) or "尚无信贷债务"
    return explanation


def audit_timeline(state: BusinessState) -> dict[str, Any]:
    """审计人员按历史时间还原全过程：不做字段裁剪，保持事件原貌与哈希顺序。"""
    timeline = []
    for event in sorted(state.events, key=lambda e: (e["occurred_at"], e["event_id"])):
        timeline.append(
            {
                "seq_phase": None,  # 由账本记录 seq；这里保留业务时间线顺序
                "event_id": event["event_id"],
                "occurred_at": event["occurred_at"],
                "event_type": event["event_type"],
                "aggregate": f"{event['aggregate_type']}/{event['aggregate_id']}",
                "version": event["version"],
                "payload": event["payload"],
            }
        )
    return {
        "business_key": state.business_key,
        "frozen": state.frozen,
        "freeze_reason": state.freeze_reason,
        "timeline": timeline,
        "链路核查": {
            "支付确认与信贷同意分离": _two_separate_decisions(state),
            "同意对应已展示要约版本": state.consent_matches_offer("credit_consent"),
            "适当性评估先于信贷同意": _suitability_before_consent(state),
            "条款变更后已重新确认": not state.pending_reconfirmation,
            "放款不超过已放本金与额度": state.disbursed <= (money(state.line["limit"]) if state.line else Decimal("0")),
        },
    }


def _two_separate_decisions(state: BusinessState) -> bool:
    payment = state.payment_confirmed
    credit = state.consents.get("credit_consent")
    if payment is None:
        return credit is None
    if credit is None:
        return True
    return payment["action_ref"] != credit.action_ref


def _suitability_before_consent(state: BusinessState) -> bool:
    if state.suitability is None or "credit_consent" not in state.consents:
        return True
    assessment = next(
        (e for e in state.events if e["event_type"] == "SUITABILITY_EVALUATED"), None
    )
    consent = next(
        (e for e in state.events if e["event_type"] == "CONSENT_CAPTURED"
         and e["payload"]["consent_kind"] == "credit_consent"),
        None,
    )
    return assessment is not None and consent is not None and assessment["occurred_at"] <= consent["occurred_at"]


def lender_view(state: BusinessState, lender_ref: str) -> dict[str, Any]:
    """放款方最小视图：只能看自己放款的业务，且只看履责所需字段。"""
    offer = state.current_offer
    if offer is None or offer.get("lender_ref") != lender_ref:
        raise AccessDenied("放款方只能查看自己作为放款方的业务")
    bills = [
        {
            "bill_seq": seq,
            "amount_due": str(b.amount_due),
            "settled": str(b.settled),
            "outstanding": str(b.outstanding),
            "currency": b.currency,
            "due_at": b.due_at.isoformat(),
            "overdue": b.overdue_noticed and b.outstanding > 0,
        }
        for seq, b in sorted(state.bills.items())
    ]
    return {
        "business_key": state.business_key,
        "lender_ref": lender_ref,
        "offer_version": offer["offer_version"],
        "disbursed": str(state.disbursed),
        "credit_available": str(state.credit_available),
        "bills": bills,
        # 刻意不含：优惠活动配置、界面内容哈希、适当性输入明细、营销信息。
        "suitability_decision_only": state.suitability["decision"] if state.suitability else None,
        "terms_current": not state.pending_reconfirmation,
    }


def collector_view(state: BusinessState, collector_ref: str) -> dict[str, Any]:
    """催收方最小视图：只看自己承办的案件与通知履责所需信息。"""
    if state.collection_case is None or state.collection_case["collector_ref"] != collector_ref:
        raise AccessDenied("催收方只能查看自己承办的案件")
    return {
        "business_key": state.business_key,
        "case_ref": f"cc-{state.business_key}",
        "collector_ref": collector_ref,
        "amount_outstanding": state.collection_case["amount_outstanding"],
        "currency": state.collection_case.get("currency"),
        "notices": [
            {"notice_seq": n["notice_seq"], "channel": n["channel"], "template_version": n["template_version"]}
            for n in state.notices
        ],
        "open_dispute": any("resolution" not in d for d in state.disputes),
        # 刻意不含：适当性原始输入、优惠配置、界面内容。
    }


def _currency_of(state: BusinessState) -> str:
    if state.line:
        return state.line["currency"]
    for bill in state.bills.values():
        return bill.currency
    return ""
