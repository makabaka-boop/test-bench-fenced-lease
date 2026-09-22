# 高压试验台租约 / 栅栏服务 (HV Bench Service)

纯后端服务：FastAPI + PostgreSQL。试验台被注册后，校准工程师通过**租约**取得
独占控制权；每次成功取得租约都会产生一个单调递增的**栅栏代次**
(`fence_generation`) 和随机租约标识 (`lease_token`)。续租、释放和受保护的
设定值写入必须同时出示持有者、租约标识和栅栏代次，且仅在租约未到期时生效。

这样，旧页面在控制权转移后**迟到的一次写入**只会得到明确的 `409` 冲突，
绝不会改变电压设定；试验台始终保持新代次下最后一次合法设定。

- 所有请求与响应均为普通 JSON（无表单、无 GraphQL）。
- 到期判断只使用数据库时间（`now()`），不依赖应用服务器或客户端时钟；
  续租的新到期时间也从当前数据库时间重新计算。
- 每个关键操作都是单条原子 SQL（行级锁 + 事务），非法操作整体回滚，
  数据库 CHECK 约束保证不会留下部分状态。
- 数据保存在 Docker 命名卷 `pgdata` 中，重启 API 或数据库后状态恢复。

## 运行

```bash
docker compose up --build
# API: http://localhost:8000  （健康检查 GET /health）
# 交互式文档: http://localhost:8000/docs
```

停止后数据仍保留在卷里；要清空：

```bash
docker compose down -v
```

### 运行测试（pytest 连接真实 PostgreSQL，无假接口/桩）

```bash
docker compose run --rm api-tests
```

本地已有 PostgreSQL 时也可直接：

```bash
export DATABASE_URL='postgresql://bench:bench@localhost:5432/benchdb'
pip install -e .                       # 或使用 uv sync
pytest
```

测试覆盖：两名客户端并发取得（恰好一方成功）、释放与写入交错、
到期后接管（含约 5 秒最小租期的实时等待）、旧持有者迟到写入被拒、
非法登记不留下行、API 重启后状态恢复。

## 数据模型

表 `benches`（一行一个试验台）：

| 列 | 说明 |
| --- | --- |
| `name` (TEXT PK) | 唯一试验台名 |
| `min_setpoint`, `max_setpoint` (INTEGER) | 允许的整数设定范围，`min <= max` |
| `current_setpoint` (INTEGER) | 当前设定值，CHECK 保证恒在范围内 |
| `fence_generation` (BIGINT IDENTITY) | 单调递增栅栏代次，仅成功取得租约时 +1 |
| `lease_token` (UUID) | 本次租约的随机标识；空闲时为 NULL |
| `holder` (TEXT) | 当前持有者；空闲时为 NULL |
| `expires_at` (TIMESTAMPTZ) | 租约到期时间（数据库时间）；空闲时为 NULL |

CHECK 约束：三个租约列要么全为 NULL、要么全非空；设定值必须在登记范围内。

## 接口

所有错误响应统一为：

```json
{ "error": { "code": "ERROR_CODE", "message": "human readable", "details": [ ... ] } }
```

`details` 仅出现在 `422 VALIDATION_ERROR`。

### 1. 登记试验台 — `POST /benches`

请求：

```json
{
  "name": "bench-A1",
  "min_setpoint": 0,
  "max_setpoint": 1000,
  "initial_setpoint": 100
}
```

`initial_setpoint` 必须在 `[min_setpoint, max_setpoint]` 内。成功返回 `201`
与试验台状态（见状态接口）。重名 → `409 BENCH_ALREADY_EXISTS`；
范围非法 / 字段缺失 → `422 VALIDATION_ERROR`。INSERT 为单语句，失败即回滚，
不会留下部分状态。

### 2. 取得租约 — `POST /benches/{name}/lease`

```json
{ "holder": "alice", "duration_seconds": 30 }
```

`duration_seconds` 为整数，范围 **5–300 秒**。

- 试验台无有效租约（从未出租 / 已释放 / 已到期）：成功 `200`，栅栏代次 +1，
  发放新的 `lease_token`，`expires_at = now() + duration`。
- 租约仍有效（`now() < expires_at`，数据库时间）：竞争者得到
  `409 LEASE_ACTIVE`。并发取得由行锁串行化，保证恰好一方成功。
- 试验台不存在：`404 BENCH_NOT_FOUND`。

### 3. 续租 — `POST /benches/{name}/renew`

```json
{
  "holder": "alice",
  "lease_token": "5499654d-c576-4ea0-baea-e8484f794a95",
  "fence_generation": 106,
  "duration_seconds": 60
}
```

必须同时匹配持有者、租约标识、栅栏代次，且租约未到期；新到期时间从
**当前数据库时间**重新计算（`now() + duration`），栅栏代次不变。
到期后续租 → `409 LEASE_EXPIRED`；凭证不匹配（含旧持有者迟到请求）
→ `409 FENCE_MISMATCH`。

### 4. 释放租约 — `POST /benches/{name}/release`

请求体同续租凭证（无 `duration_seconds`）。匹配且未到期时清空三个租约列，
返回：

```json
{ "name": "bench-A1", "released": true }
```

到期 → `409 LEASE_EXPIRED`；凭证不匹配 → `409 FENCE_MISMATCH`。

### 5. 写入受保护设定值 — `POST /benches/{name}/setpoint`

```json
{
  "holder": "alice",
  "lease_token": "5499654d-c576-4ea0-baea-e8484f794a95",
  "fence_generation": 106,
  "setpoint": 720
}
```

按顺序校验：① 试验台存在；② 租约未到期；③ 持有者/租约标识/栅栏代次
全部匹配；④ `setpoint` 为整数且在登记范围内。校验失败即回滚，
设定值保持原值。失败码分别为 `404 BENCH_NOT_FOUND`、
`409 LEASE_EXPIRED`、`409 FENCE_MISMATCH`、`422 SETPOINT_OUT_OF_RANGE`
（请求体本身非法为 `422 VALIDATION_ERROR`）。

### 6. 状态查询 — `GET /benches/{name}`

```json
{
  "name": "bench-A1",
  "min_setpoint": 0,
  "max_setpoint": 1000,
  "current_setpoint": 720,
  "leased": true,
  "lease": {
    "lease_token": "5499654d-c576-4ea0-baea-e8484f794a95",
    "holder": "alice",
    "fence_generation": 106,
    "expires_at": "2026-09-22T17:29:02.712210Z"
  }
}
```

到期与否完全由数据库时间判定；到期后 `leased=false` 且 `lease=null`
（不会暴露已失效的凭证）。另：`GET /health` 返回 `{"status":"ok"}`。

## 错误码一览

| HTTP | code | 触发条件 |
| --- | --- | --- |
| 404 | `BENCH_NOT_FOUND` | 对未登记的试验台执行任何操作 |
| 409 | `BENCH_ALREADY_EXISTS` | 登记重名 |
| 409 | `LEASE_ACTIVE` | 有效租约期内他人尝试取得 |
| 409 | `LEASE_EXPIRED` | 凭证对应当前代次但租约已到期/已释放 |
| 409 | `FENCE_MISMATCH` | 持有者、租约标识或栅栏代次与当前租约不符 |
| 422 | `VALIDATION_ERROR` | JSON 字段缺失/类型错/租期越界/UUID 非法/登记范围非法 |
| 422 | `SETPOINT_OUT_OF_RANGE` | 写入整数超出该试验台登记范围 |
| 500 | `INTERNAL_ERROR` | 未预期的服务端错误 |

> 说明：`LEASE_EXPIRED` 与 `FENCE_MISMATCH` 的区分仅为可读诊断；
> 对调用方而言二者都意味着“凭证已失效，必须重新取得租约”。

## 典型调用顺序

1. `POST /benches` —— 一次性登记试验台与允许范围。
2. `POST /benches/{name}/lease` —— 取得独占租约，记录响应中的
   `lease_token` 与 `fence_generation`。
3. `POST /benches/{name}/setpoint` —— 凭三者写设定值（可多次）。
4. 需要更久时 `POST /benches/{name}/renew`（到期前；到期时间按数据库时间重算）。
5. 结束后 `POST /benches/{name}/release`，或等待到期。
6. 任何步骤返回 `409`：重新 `GET /benches/{name}` 查看当前持有者；
   若试验台已空闲，重新走第 2 步取得**新代次**的租约。

### 控制权转移（迟到写入）时序

```
alice  ──acquire (gen=N, token=T_a)──► 写 350 ──release──┐
                                                          ▼
bob    ──────────────────────────── acquire (gen=N+1, token=T_b) ──► 写 777
alice 的旧页面迟到到达: setpoint 999, 凭证 (T_a, N)
        └─► 409 FENCE_MISMATCH；current_setpoint 仍为 777，gen 仍为 N+1
```

到期接管同理：旧代次的续租/释放/写入在 `now() >= expires_at` 后一律失败，
竞争者取得时栅栏代次前进，迟到请求无法回退代次或改写设定。
