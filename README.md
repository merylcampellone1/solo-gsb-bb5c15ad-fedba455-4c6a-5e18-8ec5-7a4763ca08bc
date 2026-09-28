# Webhook Ledger API

一个 Python (FastAPI) 账本服务：接收带 HMAC 签名的收入 / 退款 webhook 事件，按事件 ID 幂等入账，支持乱序事件的余额折叠，按账户查询余额，并可重放时间窗事件与上游系统对账。

## 快速启动（单条命令）

```bash
docker compose up --build
```

服务监听 `http://localhost:8000`，交互式 API 文档见 `http://localhost:8000/docs`。

本地开发（不用 Docker）：

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
pytest            # 运行测试
```

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `WEBHOOK_SECRET` | `dev-webhook-secret` | 验签共享密钥（生产必须覆盖） |
| `DATABASE_URL` | `sqlite:////app/data/ledger.db` | SQLAlchemy 连接串，可切换到 PostgreSQL 等 |
| `SIGNATURE_TOLERANCE_SECONDS` | `300` | 签名时间戳容忍窗口（防重放） |

## 签名方案

发送方对每个请求计算：

```
payload   = "{unix_timestamp}.{raw_request_body}"
signature = HMAC_SHA256(WEBHOOK_SECRET, payload) 的 hex 摘要
```

请求头：

```
X-Webhook-Timestamp: <unix 秒>
X-Webhook-Signature: sha256<hex 摘要>
```

时间戳超出容忍窗口（默认 ±5 分钟）或签名不匹配 → `401`。Python 生成示例：

```python
import hashlib, hmac, json, time

body = json.dumps(event).encode()
ts = str(int(time.time()))
sig = hmac.new(b"dev-webhook-secret", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
headers = {"X-Webhook-Timestamp": ts, "X-Webhook-Signature": f"sha256={sig}"}
```

## API

### 1. 接收事件 `POST /webhooks/events`

```json
{
  "event_id": "evt_001",
  "account_id": "acct-1",
  "type": "income",                
  "amount": "100.00",
  "currency": "USD",
  "occurred_at": "2026-09-20T10:00:00Z",
  "reference_event_id": null,      
  "metadata": {"order_id": "o-1"}
}
```

- 金额以十进制字符串传输，服务端转为**整数最小货币单位（分）**存储，无浮点误差；最多 2 位小数，否则 `422`。
- **幂等**：`event_id` 唯一。重复投递相同事件 → `201 {"status": "duplicate"}`，余额不重复入账；相同 `event_id` 但载荷不同 → `409 Conflict`。
- **乱序**：事件按 `occurred_at`（业务时间）记录，余额通过折叠（fold）全部事件计算——收入为正、退款为负，求和满足交换律，因此退款先于对应收入到达也会收敛到同一余额。余额投影增量维护，也可用 `GET .../balance?recompute=true` 从事件日志全量重算并自愈。

响应：

```json
{"status": "processed", "event_id": "evt_001", "account_id": "acct-1",
 "balance": {"currency": "USD", "balance": "100.00", "event_count": 1}}
```

### 2. 查询余额 `GET /accounts/{account_id}/balance[?recompute=true]`

```json
{"account_id": "acct-1",
 "balances": [{"currency": "USD", "balance": "70.00", "event_count": 3}],
 "as_of": "2026-09-20T12:00:00Z", "source": "projection"}
```

### 3. 对账 `POST /reconcile`

重放指定时间窗（按 `occurred_at`）内的账本事件，与上游系统提供的期望值逐项比对：

```json
{
  "account_id": "acct-1",
  "start": "2026-09-10T00:00:00Z",
  "end": "2026-09-10T23:59:59Z",
  "expected_balance": {"USD": "70.00"},
  "expected_events": [
    {"event_id": "w1", "type": "income", "amount": "100.00"},
    {"event_id": "w2", "type": "refund", "amount": "40.00"}
  ]
}
```

响应（`in_sync: false` 时 `discrepancies` 列出全部差异）：

```json
{"account_id": "acct-1", "window": {"start": "...", "end": "..."},
 "ledger_event_count": 3, "ledger_net": {"USD": "70.00"},
 "discrepancies": [
   {"kind": "missing_in_ledger", "event_id": "w4", "detail": "..."},
   {"kind": "missing_in_source", "event_id": "w3", "detail": "..."},
   {"kind": "amount_mismatch", "event_id": "w1", "ledger_value": "100.00", "expected_value": "90.00"},
   {"kind": "balance_mismatch", "currency": "USD", "ledger_value": "70.00", "expected_value": "65.00"}
 ],
 "in_sync": false}
```

差异类型：`missing_in_ledger`（上游有、账本无）、`missing_in_source`（账本有、上游无）、`amount_mismatch` / `type_mismatch`（同 ID 字段不一致）、`balance_mismatch`（窗口净额与期望不符）。`expected_balance`、`expected_events` 均可选。

### 4. 退款审计 `GET /accounts/{account_id}/refund-audit?as_of=2026-09-10T23:59:59Z`

按业务时间截取账本（只回放 `occurred_at <= as_of` 的事件，含 `as_of` 时刻），将退款通过 `reference_event_id` 归集到原收入，输出"每笔收入已退 / 剩余"视图以及无法归因的异常退款。

```bash
curl "http://localhost:8000/accounts/acct-1/refund-audit?as_of=2026-09-10T23:59:59Z"
```

```json
{
  "account_id": "acct-1",
  "as_of": "2026-09-10T23:59:59",
  "incomes": [
    {
      "income_event_id": "evt_001",
      "currency": "USD",
      "amount": "100.00",
      "occurred_at": "2026-09-01T10:00:00",
      "refunded_amount": "30.00",
      "remaining_amount": "70.00",
      "refunds": [
        {"event_id": "evt_002", "amount": "30.00",
         "occurred_at": "2026-09-02T09:00:00"}
      ]
    }
  ],
  "anomalies": [
    {"event_id": "evt_009", "currency": "USD", "amount": "80.00",
     "occurred_at": "2026-09-03T09:00:00", "reference_event_id": "evt_001",
     "kind": "refund_exceeds_income", "detail": "..."}
  ]
}
```

规则：

- **乱序 / 补录自愈**：同一收入的多笔退款按 `(occurred_at, event_id)` 顺序累计；审计是对 as-of 切片的纯回放，因此退款先于收入到达时，只要原收入在切片内（例如后续补录），先到的退款会**自动转为已归因**，不产生异常。
- **异常类型**（一笔退款至多一种）：
  - `reference_missing`：退款没有 `reference_event_id`；
  - `reference_not_found`：引用的事件在 as-of 账本中不存在（ID 未知，或在截止时间之后才发生 / 入账）；
  - `reference_account_mismatch`：引用事件属于其他账户；
  - `reference_currency_mismatch`：退款币种与原收入不一致；
  - `reference_not_income`：引用的事件本身是退款而非收入；
  - `refund_exceeds_income`：应用该退款会使该收入的累计已退超过原收入金额——**整笔不抵扣**收入剩余额（后续更小的退款仍可正常抵扣）。
- **排序**：`incomes` 按收入事件 ID、`anomalies` 按异常退款事件 ID 升序，回放结果稳定。
- 金额全程以整数分计算，输出为十进制字符串（如 `"70.00"`）。


## 设计要点

- **追加式事件日志 + 余额投影**：`events` 表是不可变事实源；`account_balances` 是增量维护的物化投影，两者可互相校验（`recompute=true` 全量折叠）。
- **幂等**：数据库唯一约束兜底并发重复投递；载荷哈希区分"重试"与"冲突"。
- **乱序安全**：折叠是可交换的纯函数，任意到达顺序收敛到同一余额；退款可先于收入到达。
- **金额安全**：全程整数最小货币单位运算，API 边界做十进制转换与精度校验。
- **防重放**：HMAC-SHA256 签名绑定请求体 + 时间戳容忍窗口。

## 项目结构

```
app/
  config.py      环境变量配置
  models.py      SQLAlchemy 模型（events / account_balances）
  schemas.py     Pydantic 请求/响应模型
  security.py    HMAC 签名验证
  services.py    幂等入账、余额折叠、对账核心逻辑
  routers/       webhooks / balances / reconcile / refund-audit 路由
  main.py        应用工厂
tests/test_api.py
Dockerfile  docker-compose.yml  requirements.txt
```
