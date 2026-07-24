# BE-STOR-001：Postgres 持久化 + ORM 模型 + Alembic 迁移

| 字段 | 值 |
|------|-----|
| **优先级** | 🔴 P0 |
| **域** | STOR |
| **来源** | `docs/frontend-integration.md` §7.1.6 |
| **工作量** | 3-5 天 |
| **状态** | 🚧 进行中（2026-07-24） |
| **依赖** | 无（前置） |
| **后续** | BE-AUTH-001 JWT / BE-API-001 统一响应 |

## 背景

当前 99 个端点全跑在 **模块级 in-memory dict** 上：
- `AccountLifecycle._accounts: dict[str, ParentAccount]`
- `DashboardService._reports: dict[str, WeeklyReport]`
- `TreeholeManager._summaries: dict[str, Summary]`

后果：

1. **服务重启 = 数据全丢**（家长账号、周报、对话历史、情绪基线全部蒸发）
2. **多实例无法水平扩展**（dict 在每个进程里独立）
3. **CI 测试互相污染**（共享 dict 造成 flaky）
4. **PRD #35 数据可携权无法兑现**（没有持久层就没法 export）

本工单只做**存储底座**，不动业务逻辑。业务服务（AccountLifecycle / TreeholeManager / DashboardService）的迁移在本工单内完成 **AccountLifecycle**，其余两个下个工单再做（避免单 PR 过大）。

## 验收标准（AC）

### 必需完成

- [ ] AC-1：`niki/db/models/` 下有 8 个 ORM Model，覆盖 §7.1.6 设计的 8 张表（parents / children / account_verifications / deletion_requests / weekly_reports / emotion_baselines / memory_chunks / treehole_summaries）
- [ ] AC-2：Alembic 初始化完成，首份迁移 `alembic upgrade head` 可成功建出 8 张表
- [ ] AC-3：`alembic downgrade -1` 可回滚 8 张表
- [ ] AC-4：`AccountLifecycle` 重构为使用 PostgreSQL（不再是 in-memory dict）；sign-up / get / progress / deletion / consent 五条路径全跑通
- [ ] AC-5：`pytest tests/db/ tests/auth/test_lifecycle_db.py -v` 全部通过
- [ ] AC-6：集成测试用 `sqlite+aiosqlite://` in-memory（无需 Docker），CI 友好
- [ ] AC-7：上游 `niki/infrastructure/postgres.py` 保持 backward compatible（get_engine / get_session 等接口不变）

### 不在本工单范围（明确延后）

- ❌ 业务行为变更（auth 流程保持原样）
- ❌ 重构 TreeholeManager（→ BE-STOR-001-ext-1）
- ❌ 重构 DashboardService（→ BE-STOR-001-ext-2）
- ❌ WebSocket 推送（→ BE-RT-001）
- ❌ Redis 缓存层（后续性能优化 PR）

## 设计

### 1. 包结构

```
niki/
├── db/
│   ├── __init__.py
│   ├── base.py             # Re-export Base from postgres.py
│   ├── session.py          # Async session context manager helpers
│   ├── models/
│   │   ├── __init__.py
│   │   ├── parent.py
│   │   ├── child.py
│   │   ├── account_verification.py
│   │   ├── deletion_request.py
│   │   ├── weekly_report.py
│   │   ├── emotion_baseline.py
│   │   ├── memory_chunk.py
│   │   └── treehole_summary.py
│   └── repositories/
│       ├── __init__.py
│       └── account_repo.py
alembic/
├── env.py
├── script.py.mako
└── versions/
    └── 2026_07_24_0001_initial_8_tables.py
alembic.ini                # 项目根
```

### 2. 8 张表的关键字段

| 表 | 主键 | 外键 | 关键约束 |
|---|------|------|---------|
| `parents` | id (sha256[:16]) | — | unique(name, id_number_hash); unique(phone_hash) |
| `children` | id (uuid) | parent_id → parents.id | cascade delete on parent |
| `account_verifications` | id | parent_id | unique(parent_id, step_name); 3 行 per parent (real_name / face / consent) |
| `deletion_requests` | id | parent_id | cascade delete on parent |
| `weekly_reports` | id | child_id, parent_id | idx(child_id, week_start DESC) |
| `emotion_baselines` | id | child_id | idx(child_id, recorded_at DESC) |
| `memory_chunks` | id | child_id | jsonb content; tsvector for full-text search |
| `treehole_summaries` | id | child_id | unique(child_id, week_start); encrypted_at TIMESTAMPTZ |

> 全部建索引，`JSONB` 用 PostgreSQL 特性；SQLite 测试兼容（用 `JSON` 类型 fallback）。

### 3. 状态机约束

`parents.status` 在 DB 层用 `Enum('pending','verified','suspended','deletion_pending','deleted', name='account_status')` 约束，**不允许**字符串自由出入。

### 4. AccountLifecycle 重构策略

**保留对外 API**：`start_signup` / `verify_real_name` / `verify_face` / `sign_consent` / `request_deletion` / `complete_deletion` / `get_account` / `get_progress` 签名不变。

**内部实现**：从 `dict[str, ParentAccount]` 改为 `AccountRepository`（注入 AsyncSession）。`ParentAccount`（dataclass）保留作为业务对象，新增 `Parent` ORM 类作存储对象，两边用 mapper（`to_business()` / `from_business()`）转换。

**单测不破**：现有 `tests/auth/test_lifecycle.py` 用 fake in-memory mode（依赖注入 repository=InMemoryRepository），不强制改。

### 5. Alembic 配置

- 模板：`alembic init -t async alembic`
- `env.py` 用 `niki.config.settings` 读 `database_url`
- `target_metadata = Base.metadata`（导入 `niki.db.models` 触发注册）
- 首份 migration 用手工 SQL + `op.create_table`（避免 autogenerate 在 sqlite/postgres 类型差异上的 bug）

## 风险与回滚

| 风险 | 缓解 |
|------|------|
| Alembic 首份 migration 类型与生产不一致 | dual-test：CI 跑 SQLite + 本地跑 Postgres |
| 现有测试用 module-level singleton 注入失败 | 提供 `InMemoryRepository` 实现，测试可切换 |
| 隐私数据散落在多表 | `id_number` / `phone` 在 DB 用 hash 字段（明文只在 `signup/start` 一次性使用） |
| 大表迁移时间长 | 上线用 `CREATE INDEX CONCURRENTLY`（生产 mark 注释提醒） |

## 测试矩阵

| 测试 | 文件 | 依赖 |
|------|------|------|
| ORM 模型字段完整 | `tests/db/test_models_metadata.py` | sqlite-async |
| Alembic upgrade/downgrade | `tests/db/test_alembic_roundtrip.py` | sqlite-async |
| AccountRepository CRUD | `tests/db/test_account_repo.py` | sqlite-async |
| AccountLifecycle 走 DB | `tests/auth/test_lifecycle_db.py` | sqlite-async |
| 现有 AccountLifecycle 内存模式 | `tests/auth/test_lifecycle.py`（**保留不改**） | 无 |
| 完整链路 demo | `tests/db/test_e2e_signup_deletion.py` | sqlite-async |

## 提交流程

1. 单测全绿
2. `ruff check && mypy niki/db`
3. commit：`feat(stor): BE-STOR-001 Postgres + ORM + Alembic (1/3: AccountLifecycle)`
4. 推分支 `be/BE-STOR-001`
5. PR 描述 link 到本文件

## 后续工单

- **BE-STOR-001-ext-1**：TreeholeManager 持久化
- **BE-STOR-001-ext-2**：DashboardService 持久化
- **BE-STOR-001-ext-3**：PG + Redis 双层缓存
- **BE-STOR-002**：连接保活（与本工单并行，本工单不做）
