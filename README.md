# 支付信贷授权穿透账

把支付意图、优惠条件、界面版本、信贷要约、综合融资成本、适当性输入、授权动作、合同参与方、放款、收费、还款与催收通知连接成一条**不可覆盖**的链路，解决"点了立减却莫名形成月付账单、客服四方互相推诿、无法还原当时页面与责任方"的问题。

## 核心保障

- **两个明确决定**：支付确认（`PAYMENT_CONFIRMED`）与信贷同意（`CONSENT_CAPTURED/credit_consent`）必须是两次独立显式动作；复用同一动作、默认勾选、领取优惠都不能构成信贷同意。
- **职责分离**：营销配置人员只能起草适当性规则，批准权在合规；营销角色也不能执行评估。
- **不可覆盖证据链**：事件写入 SHA-256 哈希链 JSONL，只追加；独立末端指针旁车可检出末条删除；改写金额、删行、截断都会在加载/`verify` 时暴露。
- **冲突即冻结**：同一 `business_key` 出现不同界面指纹（`ui_hash`）或金额，立即落 `FREEZE_RAISED`，冻结期间禁止一切资金动作，仅合规可解除。
- **撤销留证**：撤销未使用授信时释放额度（`CREDIT_LIMIT_RELEASED`），但授权、要约、评估等历史事件原样保留；已动用则不得撤销。
- **条款变更重签**：合同主体或费率变化落 `AGREEMENT_TERMS_CHANGED`，在 `AGREEMENT_RECONFIRMED` 之前禁止放款与扣款，且重签必须是新动作。
- **网络重试幂等**：放款、扣款携带 `idempotency_key`；重试原样返回首次结果，不重复签约/放款/扣款。
- **扣款原子裁决**：多个扣款请求争用同一可用余额时，在独占事务内"读余额—裁决—落账"，不足则落 `DEBIT_REJECTED` 留证，恰好一成一拒。
- **可控时钟**：犹豫期、账单到期、逾期、异议窗只由显式推进的时钟驱动；时钟检查点入链，重启回放后时钟不回退、阶段不重置、阶段事件不补发。
- **分角色最小视图**：消费者看债务成因/真实成本/责任方；审计按历史时间还原全过程并做链路核查；放款方只看自己放款业务的履责字段；催收方只看承办案件。

## 目录

- `contracts/domain.schema.json`：聚合、事件与每类事件必填载荷的契约目录。
- `data/sample.json`：可直接校验的联调样例。
- `src/credit_consent/`
  - `contracts.py` 信封/时区/版本/载荷契约校验（不改写输入）
  - `storage.py` 哈希链仅追加日志与末端指针、篡改检测
  - `clock.py` 只能前进的可控时钟
  - `ledger.py` 聚合版本、业务键与幂等索引、独占事务、成批原子提交、时钟检查点
  - `model.py` 事件归约得到的当前状态（可随时重建）
  - `service.py` 全部金融消费者保护不变量的强制执行点
  - `projections.py` 消费者 / 审计 / 放款方 / 催收方四类只读视图
  - `cli.py` 事件校验与哈希链验证入口
- `tests/`：契约测试 + 全链路业务不变量测试（32 个）。
- `docs/domain.md`：领域对象与事件语义。

## 测试

```bash
python3 -m unittest discover -s tests
python3 -m compileall -q src tests
```

## 命令行

```bash
# 校验单个事件
PYTHONPATH=src python3 -m credit_consent.cli contracts/domain.schema.json data/sample.json
# 校验账本哈希链完整性（输出 verified/记录数/末端哈希；被篡改返回非零）
PYTHONPATH=src python3 -m credit_consent.cli verify ledger.jsonl
```

样例有效时输出 `valid`；发现契约问题时逐行给出字段、代码和中文说明，并返回非零状态。
