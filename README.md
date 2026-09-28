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

### 4. 退款审计 `GET /accounts/{account_id}/refund-audit?as_of=...`

按业务时间截取账本（`occurred_at <= as_of`，**含 as_of 当天**），通过 `reference_event_id` 将退款归集到原收入，输出每笔收入的已退 / 剩余金额以及无法归因的退款异常。

- **乱序自愈**：同一收入的多笔退款按 `(occurred_at, event_id)` 顺序累计已退金额。退款先于原收入（补录）到达时，在较早的切片里是 `reference_not_found` 异常；原收入补录后，在包含两者的切片里自动转为已归因。
- **异常分类**（每笔退款至多一类）：

  | kind | 触发条件 |
  |---|---|
  | `missing_reference` | 退款没有 `reference_event_id` |
  | `reference_not_found` | 引用的事件在 `as_of` 切片中不存在（可能只是尚未补录） |
  | `cross_account_or_currency` | 引用的事件属于其他账户，或币种与退款不一致 |
  | `reference_not_income` | 引用的事件本身是退款而非收入 |
  | `over_refund` | 使该收入的累计退款超过原收入金额；**整笔不抵扣**收入剩余额（不做部分抵扣） |

- 结果按收入 ID 升序输出 `incomes`，异常按事件 ID 升序输出 `anomalies`；金额沿用整数分计算，以十进制字符串返回。`as_of` 必填，缺失或格式非法 → `422`。

请求示例：

```bash
curl "http://localhost:8000/accounts/acct-1/refund-audit?as_of=2026-09-20T23:59:59Z"
```

响应示例：

```json
{
  "account_id": "acct-1",
  "as_of": "2026-09-20T23:59:59Z",
  "incomes": [
    {
      "income_event_id": "evt_001",
      "currency": "USD",
      "amount": "100.00",
      "refunded_amount": "30.00",
      "remaining_amount": "70.00",
      "refund_event_ids": ["evt_002"]
    }
  ],
  "anomalies": [
    {
      "kind": "over_refund",
      "event_id": "evt_003",
      "reference_event_id": "evt_001",
      "currency": "USD",
      "amount": "80.00",
      "detail": "refund of 80.00 would raise cumulative refunds on 'evt_001' to 110.00, exceeding the income of 100.00; the whole refund is not attributed",
      "income_event_id": "evt_001",
      "income_amount": "100.00",
      "cumulative_amount": "110.00"
    }
  ]
}
```

注意：`over_refund` 退款整笔不抵扣，所以上例中 `remaining_amount` 仍是 `70.00`，而不是 `20.00`；`reference_not_found` 在收入补录后的切片中会自动消失。

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
  routers/       webhooks / balances / refund_audit / reconcile 路由
  main.py        应用工厂
tests/test_api.py
Dockerfile  docker-compose.yml  requirements.txt
```
