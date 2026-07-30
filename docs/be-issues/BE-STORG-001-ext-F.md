# BE-STORG-001-ext-F：Alembic 迁移初始化

| 字段 | 值 |
|------|-----|
| **优先级** | 🟢 P2（infra） |
| **域** | STORAGE |
| **来源** | 前端对接文档 §7.2.6（§STORAGE.003） |
| **工作量** | 0.5 天 |
| **状态** | 🚧 进行中（2026-07-30） |
| **依赖** | `niki/db/models/__init__.py` 已统一注册所有模型（13 张表） |
| **后续** | 真实部署运维流程 → DBA ticket |

## 背景

`niki/db/models/__init__.py` 已经有 13 张表（BE-STOR-001 之后所有 STORG-001-ext-*）。但 `alembic/` 目录**没有初始 migration**：

```bash
$ ls alembic/versions/
(empty)
$ alembic upgrade head
(does nothing)
```

后果：BE-STOR-001 的所有 PR 只在 `create_all()` 路径上跑过（开发 / 测试）。生产部署到干净 Postgres 时没有表。

## 验收标准

- [ ] AC-1：`alembic upgrade head` 一次性建出 13 张表
- [ ] AC-2：`alembic downgrade base` 能干净回滚
- [ ] AC-3：`alembic revision --autogenerate` 在 schema 与 head 同步时**不**产生新文件
- [ ] AC-4：测试覆盖 ≥ 3 个
- [ ] AC-5：现有 1381 passed 测试**零回归**

## 设计

`alembic/env.py` 改写：

```python
from niki.db.base import Base
from niki.db import models  # noqa: register all models

target_metadata = Base.metadata
```

初始 migration 手动写（不靠 autogenerate，因为现有表是 `create_all()` 一次性建出来的，autogenerate 推不出这个状态）。从每个模型的 `__table__` 直接 dump `create_table()` DDL。

## 后续

- 真实 CI 流程
