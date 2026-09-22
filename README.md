# 高压试验台租约与栅栏写入服务（HV Test Bench Lease Service）

纯后端服务，为远程操作高压试验台的校准工程师提供**租约（lease）+ 栅栏代次（fencing token）**保护：
只有当前有效租约的持有者才能写入电压设定值；控制权转移后，旧持有者迟到的写请求必然被拒绝，
试验台始终保持**新代次下最后一次合法设定**。

- FastAPI，全部请求与响应均为普通 JSON
- PostgreSQL 是唯一的状态存储；服务本身无内存状态，重启即可恢复
- 到期判断**只使用数据库时间**（`now()`），应用服务器时钟不参与
- 每个变更都是一条带条件的 `UPDATE ... WHERE ...`，由数据库行锁串行化，失败整体回滚，不留下部分状态
- Docker Compose 同时运行 API 与 PostgreSQL，数据保存在命名卷 `pgdata` 中

## 目录结构

```
app/            FastAPI 应用（模型 / 接口 / 错误）
tests/          连接真实 PostgreSQL 的 pytest 用例
Dockerfile
docker-compose.yml
requirements*.txt
```

## 快速开始（Docker Compose）

```bash
docker compose up --build
# API: http://localhost:8000  交互文档: /docs
```

数据库首次启动时自动建表；PostgreSQL 数据持久化在命名卷 `pgdata` 中，
`docker compose down` 不会删除数据，`docker compose down -v` 才会清除。

## 本地开发（不使用 Docker 时）

需要一个可达的 PostgreSQL，然后：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
createdb hvtest          # 或在 psql 中 CREATE DATABASE hvtest;
export DATABASE_URL='postgresql+psycopg://user:pass@localhost:5432/hvtest'
uvicorn app.main:app --reload
```

### 运行测试（必须连接真实存储）

```bash
export TEST_DATABASE_URL='postgresql+psycopg://user:pass@localhost:5432/hvtest_test'
pytest
```

`hvtest_test` 库不存在时测试夹具会自动创建；每个用例前 `TRUNCATE` 清空业务表。
测试覆盖：多个客户端并发取得（8 路屏障并发压测）、释放与写入交错、续租栅栏校验、
到期后接管、旧持有者迟到写入冲突、应用重启恢复、非法输入不留部分状态。

## 一致性模型（解决“迟到写入改变已转移控制权下的电压”）

1. 每次成功**取得**租约：生成随机令牌、`fence_generation += 1`（单调递增、永不回退）、
   `expires_at = now() + duration`（全部在数据库内计算）。
2. **续租 / 释放 / 写入**都必须在一条 SQL 中同时满足：
   `lease_token = ? AND fence_generation = ? AND lease_expires_at > now()`，
   写入还要满足登记范围 `min_setpoint <= value <= max_setpoint`。
3. 因此旧持有者在控制权转移（释放、到期被接管）后到达的请求，令牌或代次必然不匹配，
   只能得到 `409 LEASE_CONFLICT`，且当前设定值不变。
4. 到期完全由数据库时间判定，不依赖任何客户端/应用时钟，续租也从数据库当前时间重新计时。

## 接口说明

### `POST /benches` — 登记试验台

```json
{ "name": "hv-7", "min_setpoint": -500, "max_setpoint": 500 }
```

- `name`：1–64 字符，`[A-Za-z0-9_.-]` 且以字母数字开头，全局唯一
- `min_setpoint` / `max_setpoint`：整数，且 `min <= max`
- 成功 `201`，重名 `409 BENCH_EXISTS`，参数非法 `422`（记录不会被创建）

### `GET /benches` / `GET /benches/{name}` — 查询列表 / 状态

```json
{
  "name": "hv-7",
  "min_setpoint": -500,
  "max_setpoint": 500,
  "current_setpoint": 220,
  "fence_generation": 2,
  "lease": {
    "holder": "bob",
    "expires_at": "2026-09-22T16:39:36.571058Z",
    "active": true,
    "remaining_seconds": 19.6
  }
}
```

无租约时 `lease` 为 `null`；不存在返回 `404 BENCH_NOT_FOUND`。`active`/`remaining_seconds`
均按数据库当前时间实时计算。

### `POST /benches/{name}/lease/acquire` — 取得租约

```json
{ "holder": "alice", "duration_seconds": 30 }
```

- `duration_seconds`：整数，**5–300** 秒（越界 `422`）
- 无有效租约（从未取得、已释放或已到期）时成功：

```json
{
  "name": "hv-7",
  "token": "6843edc53882e102b0e9eb16390df971",
  "holder": "alice",
  "generation": 1,
  "expires_at": "2026-09-22T16:38:57.986308Z"
}
```

- 有效期内竞争者（包括持有者自己再次取得）得到 `409 LEASE_HELD`，代次不变

### `POST /benches/{name}/lease/renew` — 续租

```json
{ "token": "...", "generation": 1, "duration_seconds": 60 }
```

- 必须同时匹配令牌与代次，且租约未到期；成功返回新的 `expires_at`（从数据库当前时间起算），
  **代次不变**
- 令牌/代次/到期任一不满足 → `409 LEASE_CONFLICT`；租期非法 → `422`

### `POST /benches/{name}/lease/release` — 释放租约

```json
{ "token": "...", "generation": 1 }
```

- 条件同上；成功 `200 {"name":"hv-7","released":true}`，租约字段清空（代次保留不回退）
- 重复释放、旧代次释放、到期释放 → `409 LEASE_CONFLICT`

### `PUT /benches/{name}/setpoint` — 受保护的设定值写入

```json
{ "token": "...", "generation": 1, "value": 220 }
```

- 必须同时匹配令牌与代次、租约未到期，且 `value` 为登记范围内的整数
- 成功 `200 {"name":"hv-7","current_setpoint":220,"generation":1}`
- 租约不匹配（含旧持有者迟到写入）→ `409 LEASE_CONFLICT`
- 值超出登记范围 → `400 SETPOINT_OUT_OF_RANGE`（仅当租约本身有效时才会产生此错误，当前值不变）

### `GET /health` — 健康检查

执行 `SELECT 1`，返回 `{"status":"ok"}`。

## 错误码

错误响应统一形如 `{"error":{"code":"...","message":"..."}}`：

| HTTP | code                     | 含义                                                           |
|------|--------------------------|----------------------------------------------------------------|
| 400  | `SETPOINT_OUT_OF_RANGE`  | 写入值不在登记的整数范围内                                     |
| 404  | `BENCH_NOT_FOUND`        | 试验台未登记                                                   |
| 409  | `BENCH_EXISTS`           | 登记重名（唯一约束，含并发登记）                               |
| 409  | `LEASE_HELD`             | 取得时存在他人（或自己）的有效租约                             |
| 409  | `LEASE_CONFLICT`         | 续租/释放/写入时令牌、代次或到期条件不满足（旧请求的明确冲突） |
| 422  | （FastAPI 请求校验）     | 请求体/字段不合法；事务整体回滚，不产生任何状态                |

`LEASE_CONFLICT` 的 `message` 会进一步区分：租约已释放、已到期、令牌属于其他租约、
栅栏代次不匹配（message 仅为可读说明，客户端应依据 `code` 处理）。

## 建议调用顺序

```text
工程师 A
  1. POST /benches/{n}/lease/acquire   { holder, duration_seconds }   -> token_A, gen_A
  2. PUT  /benches/{n}/setpoint        { token_A, generation: gen_A, value }   // 可多次
  3. POST /benches/{n}/lease/renew     { token_A, generation: gen_A } // 到期前按需续租
  4. POST /benches/{n}/lease/release   { token_A, generation: gen_A } // 主动放弃

工程师 B（竞争者 / 接管者）
  1. POST .../acquire
       - 200：拿到 token_B, gen_B（gen_B = gen_A + 1），之后只有 B 能写
       - 409 LEASE_HELD：A 的租约仍有效，等待或稍后重试
  2. A 的租约到期（按数据库时间）后，B acquire 成功
  3. B 用 { token_B, generation: gen_B } 写入

网络延迟 / 控制权转移后迟到的旧请求
  - A 携带 { token_A, generation: gen_A } 的 renew/release/setpoint
    在任何时刻到达都只会得到 409 LEASE_CONFLICT；
  - 即使该写请求最后到达，current_setpoint 仍保持新代次 gen_B 下最后一次合法设定。
```
