# 领域约定

定义支付、信贷要约、适当性、授权和多主体资金责任的基础事件。

聚合对象包括`payment_intent`、`credit_offer`、`consent_record`、`fund_obligation`。事件类型包括`INTENT_CREATED`、`OFFER_PRESENTED`、`CONSENT_CAPTURED`、`FUNDS_DISBURSED`、`DISPUTE_OPENED`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `OFFER_PRESENTED`：载荷还需包含 `ui_hash`, `total_cost`。
- `CONSENT_CAPTURED`：载荷还需包含 `action_ref`, `offer_version`。
- `FUNDS_DISBURSED`：载荷还需包含 `lender_ref`, `amount`。

相同事件标识的业务幂等、冲突隔离和状态推进由上层服务负责；本仓库只定义可稳定交换的基础事实。
