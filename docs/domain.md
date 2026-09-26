# 领域约定

定义支付、信贷要约、适当性、授权、合同和多主体资金责任的基础事件。

## 聚合对象

| 聚合 | 含义 |
| --- | --- |
| `payment_intent` | 一笔支付意图（含优惠条件快照） |
| `credit_offer` | 信贷要约与界面版本 |
| `suitability_ruleset` | 适当性规则集（起草与批准分离） |
| `suitability_assessment` | 针对一笔交易的适当性评估 |
| `consent_record` | 支付确认与信贷同意两条独立授权记录 |
| `credit_line` | 授信额度及其释放 |
| `credit_agreement` | 合同主体、费率版本与重新确认 |
| `fund_obligation` | 放款、收费形成的债务 |
| `repayment_bill` | 账单与扣款 |
| `collection_case` | 催收案件与通知 |
| `dispute_case` | 用户异议 |
| `compliance_freeze` | 冲突冻结标记 |

## 事件目录

- 支付：`INTENT_CREATED`、`PAYMENT_CONFIRMED`
- 展示：`UI_VERSION_PUBLISHED`、`OFFER_PRESENTED`
- 适当性：`RULESET_DRAFTED`、`RULESET_APPROVED`、`SUITABILITY_EVALUATED`
- 授权：`CONSENT_CAPTURED`、`CONSENT_WITHDRAWN`
- 授信与合同：`CREDIT_LINE_OPENED`、`CREDIT_LIMIT_RELEASED`、`AGREEMENT_SIGNED`、`AGREEMENT_TERMS_CHANGED`、`AGREEMENT_RECONFIRMED`
- 资金：`FUNDS_DISBURSED`、`FEE_CHARGED`、`BILL_ISSUED`、`DEBIT_REQUESTED`、`DEBIT_SETTLED`、`DEBIT_REJECTED`
- 时钟阶段：`COOLING_OFF_EXPIRED`、`BILL_DUE`、`ACCOUNT_OVERDUE`、`DISPUTE_WINDOW_CLOSED`
- 催收与争议：`COLLECTION_CASE_OPENED`、`COLLECTION_NOTICE_SENT`、`DISPUTE_OPENED`、`DISPUTE_RESOLVED`
- 冻结：`FREEZE_RAISED`、`FREEZE_LIFTED`

所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

每个事件类型必填的载荷字段见 `contracts/domain.schema.json` 的 `payload_required_by_event`。关键约定：

- `INTENT_CREATED`：含 `business_key`、金额币种和 `incentive_snapshot`（领取的立减等优惠条件）。
- `OFFER_PRESENTED`：含 `ui_hash`（界面指纹）、`offer_version`、`lender_ref`、本金与 `total_cost`/`apr`/`fee_schedule`（综合融资成本）。
- `SUITABILITY_EVALUATED`：含 `ruleset_version`、`inputs_hash`、`input_fields`、`decision` 和评估角色。
- `CONSENT_CAPTURED`：`consent_kind` 区分 `payment_confirmation` 与 `credit_consent`；二者必须是两次独立的显式动作，任何默认勾选或优惠领取都不构成信贷同意；载荷携带 `action_ref`、`offer_version`、`ui_hash`。
- `FUNDS_DISBURSED` 与扣款事件携带 `idempotency_key`，网络重试不得重复签约、放款或扣款。
- 同一 `business_key` 出现不同 `ui_hash` 或金额时，由上层服务冻结该业务键。
