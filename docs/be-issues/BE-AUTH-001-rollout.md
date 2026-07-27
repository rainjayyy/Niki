# BE-AUTH-001-rollout：批量应用 get_current_parent 到受保护端点

| 字段 | 值 |
|------|-----|
| **优先级** | 🟡 P1（但实质是 P0 安全洞） |
| **域** | AUTH |
| **来源** | `docs/frontend-integration.md` §7.1.1 + §7.4 |
| **工作量** | 1 天 |
| **状态** | 🚧 进行中（2026-07-24） |
| **依赖** | BE-AUTH-001 ✅ / BE-API-001 ✅ |
| **后续** | 无 |

## 背景

BE-AUTH-001 提供了 `get_current_parent` FastAPI 依赖和演示端点 `/auth/whoami`。但**只有那一个端点**真正使用了它。其他 50+ 受保护端点（dashboard / memory / emotion / treehole / content_policy / tools / session / media / audio / chat_stream）**完全不检查 token**。

实际安全洞：
- `GET /api/v1/dashboard/alerts/{child_id}` —— 任何人都能查**任意孩子**的高危告警
- `GET /api/v1/dashboard/learning/{child_id}` —— 任何人都能读学习白皮书
- `POST /api/v1/memory/graph/entities` —— 任何人都能写认知图
- `GET /api/v1/emotion/baseline` —— 任何人都能读情绪基线
- `POST /api/v1/content-policy/blacklist/{family_id}/add` —— 任何人都能改家长黑名单

本工单批量加 `Depends(get_current_parent)` 到所有受保护端点。

## 验收标准（AC）

- [ ] AC-1：`/api/v1/dashboard/*` 全部 401（无 token）
- [ ] AC-2：`/api/v1/memory/*` 全部 401
- [ ] AC-3：`/api/v1/emotion/*` 全部 401
- [ ] AC-4：`/api/v1/treehole/*` 全部 401
- [ ] AC-5：`/api/v1/content-policy/*` 全部 401
- [ ] AC-6：`/api/v1/tools/*` 全部 401
- [ ] AC-7：`/api/v1/session/*` 全部 401
- [ ] AC-8：`/api/v1/media/upload` 401
- [ ] AC-9：`/api/v1/audio/transcribe` + `/synthesize` 401
- [ ] AC-10：`/api/v1/chat/stream` 401
- [ ] AC-11：公开端点（/auth/* signup/login/refresh/send/logout/me, /health/*, /chat/full, /openapi.json, /docs）保持 200
- [ ] AC-12：现有 986 passed 测试**零回归**（autouse override 兜底）
- [ ] AC-13：新增 `tests/auth/test_protected_routes.py` 覆盖每类端点的 401 / 200 双路径

## 设计

### 1. 哪些端点要保护

**Protected (10 类，约 50 端点)**：

| 域 | 路由 prefix | 端点数 |
|----|-----------|-------|
| Dashboard | `/api/v1/dashboard/*` | 10 |
| Memory | `/api/v1/memory/*` | 13 |
| Emotion | `/api/v1/emotion/*` | 7 |
| Treehole | `/api/v1/treehole/*` | 7 |
| Content Policy | `/api/v1/content-policy/*` | 8 |
| Tools | `/api/v1/tools/*` | 6 |
| Session | `/api/v1/session/*` | 13 |
| Media | `/api/v1/media/upload` | 1 |
| Audio | `/api/v1/audio/transcribe` + `/synthesize` | 2 |
| Chat Stream | `/api/v1/chat/stream` | 1 |
| **合计** | | **68** |

**Public (保留 anon 访问)**：

| 路由 | 原因 |
|------|------|
| `/health` + `/health/live` + `/health/ready` | 健康检查 |
| `/auth/signup/*` | 注册流程本身 |
| `/auth/sms/send` + `/auth/sms/login` | 注册即登录 |
| `/auth/refresh` | token 刷新 |
| `/auth/consent/document/*` | 同意书文本（公开文档） |
| `/auth/whoami` | 演示端点，已加 dep |
| `/chat/full` | PRD #1 匿名 demo |
| `/docs` + `/openapi.json` | Swagger |
| `/redoc` | ReDoc |

### 2. 实施策略

**Decorator 应用**：在每个 endpoint 加 `parent: ParentAccount = Depends(get_current_parent)` 参数。

**TestClient 兜底**：在 `tests/conftest.py` 加一个 autouse fixture，对所有 TestClient 测试注入 fake parent override，让 986 个现有测试**零修改**通过。

```python
@pytest.fixture(autouse=True)
def _fake_auth_for_tests():
    """Override get_current_parent so existing tests don't need to mint JWTs."""
    from niki.api.main import create_app
    # We can't override without an app instance; use a sentinel via TestClient fixture
    ...
```

实际实现思路：在 TestClient fixture 里加 `app.dependency_overrides`。

### 3. 风险

| 风险 | 缓解 |
|------|------|
| 现有测试失败 | autouse override 给所有测试注入 fake parent |
| 真的 token 没测试 | 新增 `test_protected_routes.py` 用真 JWT 测真 200 |
| /auth/me/{parent_id} 路径参数与 token sub 不匹配 | 现有实现不强制匹配；BE-AUTH-001-rollout-ext 后续工单 |
| 流式端点（SSE）+ Depends 不兼容 | FastAPI 支持，slowapi+ Depends 兼容性单独验证 |

### 4. 不在本工单

- ❌ 强制 path.parent_id == token.sub（横向越权防护）：→ BE-AUTH-001-rollout-ext
- ❌ X-Request-ID 注入：→ BE-OBS-001
- ❌ `/chat/full` 改要鉴权：保持公开

## 后续工单

- **BE-AUTH-001-rollout-ext**：横向越权防护（path.parent_id 必须等于 token.sub）
- **BE-AUTH-001-rollout-audit**：被保护端点的访问日志审计