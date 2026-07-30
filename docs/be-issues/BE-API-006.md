# BE-API-006：OpenAPI 端点描述完备

| 字段 | 值 |
|------|-----|
| **优先级** | 🟡 P1 |
| **域** | API / 文档 |
| **来源** | 前端 SDK 生成器（TypedDict 注释均为空；Swagger UI 不可读）；BE-API-007（TS SDK 自动生成）的前置 |
| **工作量** | 0.5 天 |
| **状态** | ✅ 完成（2026-07-30） |
| **依赖** | BE-API-001 ✅（unified response shape §6.3） |
| **后续** | BE-API-007（TS SDK 自动生成） |

## 背景

前端家长端从 FastAPI 的 `/openapi.json` 自动生成 TS 类型 + 注释。
目前实际打开 `docs/api-endpoints.md` / Swagger UI 看到的是：

- 每个端点的 `tags` 仅是名字（无 description），Swagger 分组像空白
- 大部分 `@router.get(...)` 缺少 `summary`，只有 docstring → Swagger UI 看上去空白
- 完全没有 `description` 字段（FastAPI 默认会把 docstring 当 description，但前面空行 / 缩进 / Args: section 都让生成的 HTML 不可读）
- 完全没有 `examples` 字段（前端 SDK 在 TSDoc 里看不到请求体长什么样）

直接后果：**TS 类型能编译**（types-only OK），但 TS 文件里 IDE 悬浮只看到 `(...): Promise<any>` —— 编程体验跟手写 `.d.ts` 没差。

本工单做两件事：

1. **一次性 sweep**：把每个端点 decorator 都补上 `summary` + `description`（取自 docstring 第一段），给每个 router 的 `tags=[...]` 元素加 description
2. **CI 卡点**：`tests/api/test_openapi_docs.py` 静态分析 `/openapi.json`，未来谁漏写直接挂

## 验收标准（AC）

- [x] AC-1：每个 `@router.{get,post,put,delete,patch}` 的 OpenAPI 节点都包含非空 `summary` 和非空 `description`
- [x] AC-2：每个 OpenAPI 节点的 `tags` 至少有一个，且 tag 名字在 `openapi.tags` 全局段落里有 `description`（Swagger UI 分组可见）
- [x] AC-3：每个端点的 200 响应（成功路径）都至少有一个 `content[application/json].schema`
- [x] AC-4：接受 JSON body 的 `POST/PUT/PATCH` 端点都有 `requestBody.content[application/json].schema`（不只是空 body）
- [x] AC-5：`tests/api/test_openapi_docs.py` 通过（`pytest -q --no-header` 现有 1553 + 新增 ≥3 全绿）
- [x] AC-6：`scripts/gen_openapi_table.py` 新增「覆盖率」段落——按 tag 汇总 `summary-present` / `summary-missing` 计数，写入 `docs/api-endpoints.md`
- [x] AC-7：**零回归**——`pytest -q --no-header` 现有 1553 passed / 2 skipped 不变

## 设计

### 1. sweeper 的做法

**不动路由行为**。只加 metadata：

```python
# 之前
@router.post("/signup/start")
async def signup_start(request: Request, payload: dict) -> dict:
    """Begin signup. Creates a PENDING account."""
    ...

# 之后
@router.post(
    "/signup/start",
    summary="Begin signup",
    description=(
        "Begin parent signup. Creates a PENDING account and returns "
        "the parent's progress through the multi-step verification flow."
    ),
)
async def signup_start(request: Request, payload: dict) -> dict:
    """Begin signup. Creates a PENDING account."""
    ...
```

规则：

- `summary` = docstring 第一段第一个句号前的短语（标题句式，无句号）
- `description` = 把 docstring 主体多行串起来，单一长段落（FastAPI 会渲染成 HTML <p>）
- 单行 docstring 时 description = summary（即不为同一句话生成两遍）
- 已有 `summary=` / `description=` 的端点**不动**（手工维护优先）

每个 module 顶部的 `router = APIRouter(prefix="/auth", tags=["auth"])` 改成：

```python
auth_tags_metadata = [
    {"name": "auth", "description": "Parent authentication (PRD #30, #35, #36)."},
]
router = APIRouter(prefix="/auth", tags=auth_tags_metadata)
```

但 `tags=` 接受的是 `list[str | dict[str, str]]`——FastAPI 0.95+ 支持 dict 元素（带 `name` + `description`）。
这些 dict 也需要在 `niki/main.py` 的 `create_app` 里通过
`app.openapi_tags = [...]` 注册到全局 `openapi.tags` 段（这步在
`create_app` 里一并做，全局 metadata 合并一次）。

### 2. 测试（静态分析）

```python
def test_openapi_has_summary_description_tags():
    app = create_app()
    schema = app.openapi()
    for path, methods in schema["paths"].items():
        for method, info in methods.items():
            if method.lower() not in {"get","post","put","delete","patch"}:
                continue
            assert info.get("summary"), f"{method} {path} missing summary"
            assert info.get("description"), f"{method} {path} missing description"
            assert info.get("tags"), f"{method} {path} missing tags"

def test_openapi_200_has_json_schema():
    ... 每个 path × method 的 responses["200"].content["application/json"].schema

def test_openapi_post_has_request_body_schema():
    ... POST/PUT/PATCH 都要 requestBody.content[application/json].schema

def test_openapi_tags_have_descriptions():
    tags_global = schema.get("tags", [])
    names = {t["name"] for t in tags_global}
    # 所有 info["tags"] 中的 tag 名字都在全局 tags 里有 description
```

走 `app.openapi()` —— FastAPI 在内存里 build schema，**不需要起 server**，也不依赖 DB。TestClient 不需要。

### 3. gen_openapi_table.py 增量

现有脚本只 print endpoints，现在在 「分布」表格后面加一个「覆盖率」表格：

```
| Tag | Endpoints | With summary | Missing summary |
|---|---|---|---|
| auth | 11 | 11 | 0 |
| dashboard | 10 | 10 | 0 |
```

**门槛**：所有数字都该为 `Missing=0`。CI 不会跑这个，但脚本本身是事实的 source of truth，PR reviewer 一眼看到漏写。

### 4. 风险 & 缓解

| 风险 | 缓解 |
|------|------|
| 改 decorator 顺序引起 lint 告警 | `summary/description` 是 kwarg，IDE 重排无关 |
| summary 用 docstring 自动生成导致坏句式 | docstring 第一段已经手写好；本工单不自动改 docstring，只读 |
| 把 tags 改成 dict 形式让 FastAPI 拒绝 | FastAPI 0.95+ 已支持；查 pyproject.toml 里 `fastapi>=0.110` |
| 测试 fixture 引到 DB / Redis | 测试只调用 `create_app()` + `app.openapi()`，不命中 endpoint handler，纯 metadata 序列化 |

### 5. 不在本工单

- ❌ 自动生成 `examples`（要读 service 层 mock 数据；先以现 docstring 提供的描述为准，后续工单再加 examples）
- ❌ `openapi_extra` / `responses` 错误码补全（错误响应走 §6.3 统一 shape，不在 OpenAPI schema 里展开）
- ❌ 重构 docstring 为 Google/NumPy 风格（只 sweep metadata，不动 docstring）

## 后续

- **BE-API-007**：基于现在的 OpenAPI schema 生成 TS SDK（`openapi-typescript` + 自定义 fetcher wrapper）
- **BE-API-008**（可选）：给每个端点补 `examples` payload（基于已有 __init__.py mock data）
