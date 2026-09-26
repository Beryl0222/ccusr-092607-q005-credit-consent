# 领域约定

定义支付、信贷要约、适当性、授权和多主体资金责任的全链路事实。`case_id` 贯通一笔消费从支付意图到催收、异议的全过程；`repayment_account` 与 `governance` 是跨案件聚合，不携带 `case_id`。

## 聚合

| 聚合 | 含义 |
| --- | --- |
| `case_file` | 案件阶段与通知、异议（案件状态机的载体） |
| `payment_intent` | 支付意图、优惠事实、支付确认（决定一） |
| `credit_offer` | 信贷要约版本、界面指纹、综合融资成本、适当性评估 |
| `consent_record` | 两类独立授权：`payment_confirmation` 与 `credit_agreement` |
| `credit_contract` | 合同签署、主体/费率变更的重确认要求、未使用授信撤销 |
| `fund_obligation` | 放款、收费、出账、扣款、还款 |
| `repayment_account` | 扣款账户可用余额（跨案件共享） |
| `governance` | 经合规角色批准的适当性规则版本 |

## 事件

`INTENT_CREATED`、`PROMO_APPLIED`、`PAYMENT_CONFIRMED`、`OFFER_PRESENTED`、`SUITABILITY_RULE_PUBLISHED`、`SUITABILITY_ASSESSED`、`CONSENT_CAPTURED`、`CONTRACT_SIGNED`、`RECONFIRMATION_REQUIRED`、`CREDIT_REVOKED`、`FUNDS_DISBURSED`、`FEE_CHARGED`、`REPAYMENT_DUE`、`BALANCE_REGISTERED`、`DEDUCTION_REQUESTED`、`REPAYMENT_MADE`、`PHASE_ADVANCED`、`NOTICE_SENT`、`DISPUTE_OPENED`、`DISPUTE_RESOLVED`、`CASE_FROZEN`。

所有发生时间必须携带时区，聚合版本从 1 开始严格连续递增；基础校验不改写调用方输入。事件载荷必填项与枚举见 `contracts/domain.schema.json` 的 `payload_required_by_event`、`payload_enums`、`payload_enums_by_event`。

## 上层服务强制的不变量

`src/credit_consent/domain.py` 在基础契约之上执行业务规则：

1. **两个明确决定**：支付确认（`payment_confirmation`）与信贷同意（`credit_agreement`）是两条独立的 `CONSENT_CAPTURED` 记录、不同的 `action_ref`。信贷同意前必须已存在支付确认；优惠（`PROMO_APPLIED`）只是营销事实，`explicit != true` 的同意一律拒绝——默认值、预勾选、优惠都不能代替授权。
2. **适当性职责分离**：规则只能由 `compliance_officer` 发布（营销配置人员被拒绝）；评估只引用已发布的规则版本，载荷保存规则版本、评估角色与输入哈希；结论非 `eligible` 不得取得信贷同意。原始输入不落案件事件，放款方视图只能看到输入哈希。
3. **绑定具体版本**：同意必须指向已展示的 `offer_version`、`ui_hash`、`terms_hash`。主体或费率变化产生 `RECONFIRMATION_REQUIRED`，旧条款哈希上的同意即刻失效，新要约未重新显式同意前不得签约/放款。
4. **撤销留痕**：`CREDIT_REVOKED` 仅允许在未放款时发生，记录释放额度并推进到 `revoked`；展示、评估、授权、合同事件全部保留，撤销后不得放款。
5. **重试安全与即时冻结**：`business_key` 命中且 `request_fingerprint` 一致时返回首次结果，绝不重复签约、放款、扣款；同键不同指纹立即落 `CASE_FROZEN`（界面类冲突为 `fingerprint_conflict`，资金类为 `amount_conflict`）并推进 `frozen`，冻结后一切资金动作停止。
6. **余额原子争用**：`request_deduction` 在账本临界区内完成"读余额—裁决—落事件"，并发请求最多一笔 `settled`，其余记 `insufficient_funds`，结果随事件持久化。
7. **可控时钟**：犹豫期（签约后 7 天）、账单日、逾期、异议期（出账后 30 天）与催收通知全部由 `ControlledClock` 驱动；阶段由事件流投影，进程重启只重放事件，阶段不重置。

## 账本

`src/credit_consent/ledger.py`：只追加 JSONL，每条记录含 `prev_hash` 与内容哈希；追加时校验契约、聚合版本连续与业务键唯一，重放时逐行重建并验链，任何覆盖、删改都抛 `LedgerIntegrityError`。

## 只读视图

`src/credit_consent/readmodel.py`：

- `consumer_explanation`：回答"为何形成债务、真实成本、谁负责什么"，并列两个独立决定与责任方（支付平台 / 助贷机构 / 放款方 / 收费方 / 催收方）。
- `audit_timeline(as_of=...)`：按历史时间还原展示、授权、评估、资金、催收全过程，可指定历史时点。
- `lender_view`：按主体隔离，放款方只能看到履责所需的合同条款、放款、费用、回收与适当性结论（原始输入仅哈希）。
