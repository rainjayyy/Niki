# BE-COMP-002：时区一致（扫尾 utcnow() 残留）

| 字段 | 值 |
|------|-----|
| **优先级** | 🟠 P0（合规） |
| **域** | COMPLIANCE |
| **来源** | 前端对接文档 §7.1.8 |
| **工作量** | 0.5 天 |
| **状态** | ✅ 已完成（2026-07-29） |
| **依赖** | — |
| **后续** | 监控 + 业务时间（业务时间 vs UTC 区别）→ BE-COMP-002-rollout |

## 背景

BE-COMP-002 之前是 ⚠️ 部分完成：新增代码用 `datetime.now(UTC)`，但老代码仍有 74 处 `datetime.utcnow()` 调用。

后果：
- 前端对 timestamp 转本地时区（`dayjs(t).local()`）时，naive datetime 被当成**本地时间**而非 UTC
- 东八区家庭在跨时区对话时会出现 ±8h 偏差
- 同一 `created_at` 字段在不同行可能用不同方法生成（混用 `utcnow` + `now(UTC)`），不可比

## 验收标准（AC）

- [x] AC-1：全工程零 `datetime.utcnow()` 调用
- [x] AC-2：所有文件 `from datetime import ... timezone` 覆盖
- [x] AC-3：`window_stats(today=naive)` 自动 attach UTC
- [x] AC-4：测试 1226 passed，零回归

## 设计

### 1. 全工程 sweep

脚本式替换：
- `datetime.utcnow()` → `datetime.now(timezone.utc)`
- `field(default_factory=datetime.utcnow)` → `field(default_factory=lambda: datetime.now(timezone.utc))`
- 自动为每个文件的 `from datetime import ...` 添加 `, timezone`

### 2. 防御性兼容

`EmotionBaseline.window_stats(today)` 接受可选 `today` 参数。当 caller 传 naive datetime（legacy tests + scripts），自动 attach UTC：

```python
if today is None:
    today = datetime.now(timezone.utc)
elif today.tzinfo is None:
    today = today.replace(tzinfo=timezone.utc)
```

让外部调用者不会因为时区差异而 break。

### 3. 改动文件

- **22 niki/ 文件**：agents / auth / content_policy / dashboard / memory / resilience / session / treehole / db/repositories
- **9 tests/ 文件**：测试用同样的 sweep 升级

## 后续工单

- **BE-COMP-002-rollout**：业务时间 vs UTC 区分（PRD 提到的"孩子当地时间 21:00" vs 服务器 UTC）
- **BE-COMP-003**：最低客户端版本强制
- **BE-COMP-005**：数据删除凭证
