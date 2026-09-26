"""只读视图：消费者解释、审计历史还原、放款方最小可见数据。

视图不持有状态，全部从不可变事件流现场投影，保证看到的内容与证据一致。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from .domain import D, CaseState, project_case


PARTY_RESPONSIBILITY: dict[str, str] = {
    "payment_platform": "支付平台：支付意图、支付确认页面与扣款通道",
    "loan_facilitator": "助贷机构：信贷要约展示、适当性信息采集与导流",
    "lender": "放款方：授信审批、放款、本金与利息债权",
    "creditor": "收费方：对应费用项目的债权主体",
    "collector": "催收方：逾期后的通知与催收执行",
}


def _events_as_of(events: list[dict[str, Any]], as_of: datetime | None) -> list[dict[str, Any]]:
    if as_of is None:
        return list(events)
    return [e for e in events if datetime.fromisoformat(e["occurred_at"]) <= as_of]


def _sum(items: list[dict[str, Any]], key: str) -> Decimal:
    return sum((D(i[key]) for i in items), Decimal("0"))


def consumer_explanation(events: list[Mapping[str, Any]]) -> dict[str, Any]:
    """回答消费者三问：这笔钱为何变成债务、真实成本是多少、谁负责什么。"""
    state = project_case(list(events))
    offer = state.offers[state.latest_offer_version] if state.latest_offer_version else None
    credit = state.consents.get("credit_agreement")
    payment = state.consents.get("payment_confirmation")
    assessment = state.assessments[credit.offer_version] if credit and credit.offer_version in state.assessments else None

    bills_total = _sum(state.bills, "due_amount")
    repaid_total = _sum(state.repayments, "amount")
    settled_deductions = sum(
        (D(d["amount"]) for d in state.deductions if d["outcome"] == "settled"), Decimal("0")
    )
    outstanding = bills_total - repaid_total - settled_deductions

    decisions = []
    if payment:
        decisions.append(
            {
                "决定": "支付确认",
                "动作标识": payment.action_ref,
                "界面指纹": payment.ui_hash,
                "显式作出": True,
            }
        )
    if credit:
        decisions.append(
            {
                "决定": "信贷同意",
                "动作标识": credit.action_ref,
                "要约版本": credit.offer_version,
                "界面指纹": credit.ui_hash,
                "条款哈希": credit.terms_hash,
                "显式作出": True,
            }
        )

    return {
        "案件标识": state.case_id,
        "当前阶段": state.phase,
        "消费金额": state.intent["amount"] if state.intent else None,
        "享受的优惠": (
            {"优惠": state.promo["promo_ref"], "条件": state.promo["condition_text"], "立减金额": state.promo["discount_amount"]}
            if state.promo
            else None
        ),
        "两个独立决定": decisions,
        "决定是否分离": payment is not None and credit is not None and payment.action_ref != (credit.action_ref if credit else None),
        "为何形成债务": (
            "用户对已展示的具体信贷要约版本作出了独立的显式信贷同意，合同签署后放款方履行了放款"
            if credit and state.contract and state.disbursed
            else "尚未形成信贷债务（缺少同意、合同或放款中的某一环）"
        ),
        "真实成本": (
            {
                "本金": offer.principal,
                "综合融资成本(含全部费用)": offer.total_cost,
                "年化利率(APR)": offer.apr,
                "费用明细": offer.fee_breakdown,
                "实际已记账费用": [{"类型": f["fee_type"], "金额": f["amount"], "收费方": f["creditor_ref"]} for f in state.fees],
                "还款计划": offer.repayment_schedule,
            }
            if offer
            else None
        ),
        "负担能力判断": (
            {
                "规则版本": assessment["rule_version"],
                "评估执行者": assessment["assessor_role"],
                "结论": assessment["decision"],
                "输入证据哈希": assessment["inputs_hash"],
            }
            if assessment
            else None
        ),
        "合同与参与方": (
            {
                "合同号": state.contract["contract_ref"],
                "签署要约版本": state.contract["offer_version"],
                "条款哈希": state.contract["terms_hash"],
                "参与方": [
                    {"角色": p["role"], "主体": p["ref"], "职责": PARTY_RESPONSIBILITY.get(p["role"], p["role"])}
                    for p in state.contract["parties"]
                ],
            }
            if state.contract
            else None
        ),
        "放款": [{"放款方": d["lender_ref"], "金额": d["amount"]} for d in state.disbursed],
        "账单与偿还": {
            "已出账总额": str(bills_total),
            "已主动还款": str(repaid_total),
            "已成功扣款": str(settled_deductions),
            "当前应还": str(outstanding),
        },
        "撤销状态": (
            {"已撤销未使用授信": True, "释放额度": state.revoked["released_amount"]} if state.revoked else None
        ),
        "冻结原因": state.frozen_reasons,
        "通知记录": [{"类型": n["notice_kind"], "渠道": n["channel"], "模板": n["template_ref"]} for n in state.notices],
        "异议": [{"类型": d["event_type"], "说明": d.get("reason") or d.get("resolution")} for d in state.disputes],
    }


def audit_timeline(
    events: list[Mapping[str, Any]], as_of: datetime | None = None
) -> list[dict[str, Any]]:
    """按历史时间（或指定历史时点）还原展示、授权、评估、资金与催收全过程。"""
    selected = _events_as_of([dict(e) for e in events], as_of)
    timeline: list[dict[str, Any]] = []
    for event in sorted(selected, key=lambda e: (e["occurred_at"], e["event_id"])):
        body = event["payload"]
        entry = {
            "时间": event["occurred_at"],
            "事件": event["event_type"],
            "聚合": f'{event["aggregate_type"]}:{event["aggregate_id"]}',
            "版本": event["version"],
        }
        if "ui_hash" in body:
            entry["界面指纹"] = body["ui_hash"]
        if "offer_version" in body:
            entry["要约版本"] = body["offer_version"]
        if "terms_hash" in body:
            entry["条款哈希"] = body["terms_hash"]
        if "business_key" in body:
            entry["业务键"] = body["business_key"]
        if event["event_type"] == "CASE_FROZEN":
            entry["冻结原因"] = body["reason"]
            entry["证据"] = body["evidence"]
        timeline.append(entry)
    return timeline


def lender_view(events: list[Mapping[str, Any]], viewer_ref: str) -> dict[str, Any]:
    """放款方只能看到履责所需数据：合同条款、放款、还款与适当性结论。

    看不到：消费者收入等原始适当性输入（仅留哈希）、支付侧动作细节、
    优惠配置、其他主体的内部标识，以及不属于自己的案件。
    """
    state = project_case(list(events))
    offer = state.offers[state.latest_offer_version] if state.latest_offer_version else None
    if offer is not None and offer.lender_ref != viewer_ref:
        raise PermissionError("放款方无权查看非本机构放款的案件")
    if state.contract is not None:
        party_refs = {p["ref"] for p in state.contract["parties"] if p["role"] == "lender"}
        if party_refs and viewer_ref not in party_refs:
            raise PermissionError("放款方与合同参与方不匹配")

    credit = state.consents.get("credit_agreement")
    assessment = state.assessments[credit.offer_version] if credit and credit.offer_version in state.assessments else None
    settled = sum((D(d["amount"]) for d in state.deductions if d["outcome"] == "settled"), Decimal("0"))
    failed = [d["request_ref"] for d in state.deductions if d["outcome"] == "insufficient_funds"]

    return {
        "案件标识": state.case_id,
        "当前阶段": state.phase,
        "合同": (
            {
                "合同号": state.contract["contract_ref"],
                "条款哈希": state.contract["terms_hash"],
                "本金": state.contract["principal"],
                "综合融资成本": state.contract["total_cost"],
                "还款计划": offer.repayment_schedule if offer else None,
            }
            if state.contract
            else None
        ),
        "适当性": (
            {"规则版本": assessment["rule_version"], "结论": assessment["decision"], "输入哈希": assessment["inputs_hash"]}
            if assessment
            else None
        ),
        "本机构放款": [{"金额": d["amount"], "时间锚定事件": d["business_key"]} for d in state.disbursed if d["lender_ref"] == viewer_ref],
        "本机构费用": [{"类型": f["fee_type"], "金额": f["amount"]} for f in state.fees if f["creditor_ref"] == viewer_ref],
        "回收情况": {
            "已出账": str(_sum(state.bills, "due_amount")),
            "已还款": str(_sum(state.repayments, "amount") + settled),
            "失败扣款请求": failed,
        },
        "冻结": state.frozen_reasons,
    }
