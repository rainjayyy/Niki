# BE-API-007：TypeScript SDK 自动生成

| 字段 | 值 |
|------|-----|
| **优先级** | 🟡 P1 |
| **域** | API / 前端工具链 |
| **来源** | 前端家长端手写 `fetch()` + `type` ×138 端点；后端每次改 shape 都要一个前端 PR |
| **工作量** | 1 天 |
| **状态** | ✅ 完成（2026-07-30） |
| **依赖** | BE-API-006 ✅（每个端点都有 `summary` / `description` / `tags`，生成器才有东西可读） |
| **后续** | BE-API-008（multipart body 支持：`/media/upload`、`/media/avatar`、`/media/ocr`、`/audio/transcribe`） |

## 背景

前端目前**每个端点手写一遍** `fetch()` + 手写一遍返回类型：

```ts
// frontend/src/api/dashboard.ts —— 手写版
export async function getWeeklyReport(childId: string) {
  const r = await fetch(`/api/v1/dashboard/weekly-report/${childId}`, {
    headers: { Authorization: `Bearer ${token()}` },
  });
  if (!r.ok) throw new Error(await r.text());
  return (await r.json()) as WeeklyReport;   // ← 手写 interface，跟后端漂移
}
```

规模：**138 个端点 / 27 个 router**。后果：

- 后端改一个字段名 → 前端 TS 类型不报错（`as` 是断言不是校验）→ 运行时才炸
- 新增端点 → 前端要手写 fetch + 类型 + 错误分支，一个端点 15 行样板
- `Authorization` header / `AbortSignal` / query-string 拼接每个文件重复一遍

BE-API-006 已经把 OpenAPI schema 补全（每个端点 `summary` + `description` + 有
description 的 `tags`）。本工单把这份 schema 变成**可编译的 TS 客户端**，
一条命令生成，前端只 import 不手写。

**零依赖是硬约束**：不引 `openapi-typescript-codegen` / `openapi-typescript` /
`orval`。理由：(1) 这些包会把 npm 依赖树引进后端仓库的 CI；(2) 生成结果
夹带 runtime helper（`request.ts` / `OpenAPI` 全局单例 / axios），前端要为
生成器的抽象买单；(3) 我们只需要 `fetch` + `interface`，手写 emitter 约
400 行 Python stdlib 就够，且输出完全可读、可 review、可 diff。

## 验收标准（AC）

- [x] AC-1：`python -m scripts.gen_ts_sdk` 与 `make ts-sdk` 都能生成 SDK，退出码 0，零外部依赖（stdlib + 已装 FastAPI）
- [x] AC-2：按 `tags` 分组 —— 每个 tag 一个文件 `frontend/src/api/<tag>.ts`（27 个 tag → 27 个文件 + `client.ts` / `models.ts` / `index.ts` = 30）
- [x] AC-3：每个方法是 `async function`，返回 `Promise<T>`；path 参数为位置参数，query / body / `options` 为可选参数
- [x] AC-4：类型从 `components.schemas` 推导，写入 `frontend/src/api/models.ts`（含 `HealthResponse` / `HealthStatus` / `ValidationError` / …）
- [x] AC-5：`frontend/src/api/index.ts` re-export 所有 tag 模块 + `models` + `client`（运行时 helper）
- [x] AC-6：**确定性输出** —— 连续两次生成，除 header 时间戳外逐字节相同（`--no-timestamp` 下完全相同）
- [x] AC-7：生成物覆盖全部 139 个 operation（工单起草时前端 handoff 记的是 138；实测 `app.openapi()` 为 139，测试**从 schema 现算**不写死），每个 tag ≥ 1 个方法
- [x] AC-8：`tsc --noEmit` 可用时通过；不可用时退化为结构性断言（本机默认无 node toolchain，见「设计 §5」。装 `typescript@5` 后实测 `tsc --noEmit --strict --target ES2020` **退出码 0**）
- [x] AC-9：`tests/scripts/test_gen_ts_sdk.py` 新增 69 个测试全绿（无 toolchain 时 67 passed / 2 skipped）
- [x] AC-10：**零回归** —— 既有测试 `pytest -q --no-header --ignore=tests/scripts` **1609 passed / 2 skipped** 全绿（工单起草时引用的 1576 是 BE-API-006 之前的旧基线）；含新测试全量为 **1676 passed / 4 skipped**
- [x] AC-11：README 记录生成命令 + 一段说明

## 设计

### 1. 数据流

```
niki.main:app ──app.openapi()──▶ dict (in-process, 不起 server)
                                   │
                    ┌──────────────┴───────────────┐
                    ▼                              ▼
          components.schemas               paths{}.{get,post,…}
                    │                              │
                    ▼                              ▼
             models.ts (interface)      按 tags[0] 分桶 → <tag>.ts
                    └──────────────┬───────────────┘
                                   ▼
                        index.ts (re-export) + client.ts (fetch helper)
```

关键：**generator 不 import 任何 niki 内部模块**（除了 `niki.main`）。
它只吃一个 OpenAPI dict。`build_sdk(schema) -> dict[filename, contents]`
是纯函数 —— 测试可以喂一个 12 行的 fake schema，不用 boot app。

### 2. 命名规约

| 来源 | 规则 | 例 |
|------|------|-----|
| 方法名 | **verb + path 的字面段**（不含 path 参数）→ camelCase；重名时追加 `By<Param>`，再撞则加数字后缀 | `GET /api/v1/dashboard/weekly-report/{child_id}` → `getDashboardWeeklyReport` |
| 文件名 | tag 名 slugify（`content-policy` → `content-policy.ts`，`auth_audit` → `auth-audit.ts`） | |
| 类型名 | `components.schemas` 的 key，非法字符转 `_`；inline response schema → `<MethodName>Response` | `HealthResponse` |

**为什么不用 `operationId`**：FastAPI 自动生成的
`weekly_report_api_v1_dashboard_weekly_report__child_id__get` 里 path 参数
名是**嵌在标识符中间的**（`__child_id__`），去尾巴要靠脆弱的字符串规则，
而且函数改名会连带改 SDK 方法名。改用 verb + 字面 path 段派生：**path 参数
不参与命名**，所以给端点新增一个参数不会重命名已有方法（非破坏性）。

实测 139 个 operation 派生出 139 个唯一方法名、0 冲突。仍保留两级去重兜底
（先 `By<Param>`、再数字后缀），让输出永远可编译，而不是静默覆盖丢方法。

### 3. 生成的方法形状

```ts
/**
 * Get the weekly emotion report (PRD #24)
 *
 * Get the weekly emotion report (PRD #24).
 *
 * @remarks `GET /api/v1/dashboard/weekly-report/{child_id}`
 */
export async function getDashboardWeeklyReport(
  childId: string,
  options?: RequestOptions,
): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>(
    "GET",
    `/api/v1/dashboard/weekly-report/${encodeURIComponent(String(childId))}`,
    { ...options },
  );
}
```

（以上是 `frontend/src/api/dashboard.ts` 里逐字的真实产物。）

参数顺序：**必填 path 参数（按 URL 出现顺序）→ body → 可选 query 对象 → `options`**。
可选 query 收进一个 object 而不是散成 N 个可选位置参数 —— 后端加一个新
query 参数不会改变已有调用方的实参位置（**非破坏性演进**）。

TS 不允许「可选参数后面跟必填参数」。当后面的参数确实必填时（例如
body 可选但存在必填 query），生成器把前面的可选参数放宽成
`T | undefined` 而不是 `?:` —— 调用点仍需显式传 `undefined`，但输出
始终可编译。有专门的测试 `test_no_required_param_after_optional` 守这条。

### 4. 运行时 helper（`client.ts`，唯一手写运行时）

一个文件、约 90 行、零依赖，暴露：

```ts
export interface RequestOptions { signal?: AbortSignal; headers?: Record<string,string>; baseUrl?: string; }
export function configure(opts: { baseUrl?: string; getToken?: () => string | null | undefined }): void;
export class ApiError extends Error { status: number; code?: string; detail?: unknown; }
export async function request<T>(method, path, init): Promise<T>;
```

`request` 负责：baseUrl 拼接、query-string 序列化（跳过 `undefined`）、
`Authorization: Bearer` 注入（走 `configure({getToken})`，不是全局可变
单例 token）、按 §6.3 error shape 解 `{error:{code,message}}` 抛 `ApiError`、
`204` / 空 body 返回 `undefined as T`。

**这是唯一有行为的文件**，所以它是模板字面量常量，不是拼出来的 ——
review 时看到的就是最终 TS。

### 5. 测试策略（`tsc` 不可用的现实）

本机与 CI 都没有 node toolchain（实测 `npx tsc` → `missing packages`）。
所以测试分两层：

1. **`tsc --noEmit` 机会主义**：`shutil.which("tsc")` / `npx --no-install tsc`
   探测；找到就真编译（含临时 `tsconfig.json`，target ES2020 / strict），
   找不到就 `pytest.skip`。CI 装了 node 自动升级为真编译，不装也不红。
2. **结构性断言（永远跑）**：括号 / 花括号配平、每个 `export async function`
   都有 `: Promise<` 返回标注、无 `any`、无 `TODO`、import 的 symbol 都在
   目标文件里 export、方法名唯一、138 个 operation 全覆盖、每 tag ≥1 方法。

确定性用 `--no-timestamp` 跑两遍比对 dict 相等（不落盘），另外落盘跑一遍
确认文件内容也一致。schema 的 key 迭代顺序在 FastAPI 里由路由注册顺序决
定（稳定），但生成器**仍对 tag / 方法 / 类型显式排序**，不依赖 dict 顺序。

3. **`client.ts` 运行时行为测试**（`TestClientRuntimeBehaviour`）：结构断言
   证明不了「bearer header 落在正确的地方」或「§6.3 error body 变成填好的
   `ApiError`」。所以另有一个测试把 `client.ts` + 一个注入 fake `fetch` 的
   driver 一起编译再用 node 跑，逐条校验：query 序列化跳过 `undefined`/`null`
   并展开数组、`buildUrl` 拼接、bearer 注入、JSON `Content-Type`、400 →
   `ApiError{status,code,message,requestId}`、空 body → `undefined`、
   **FormData 不设 `Content-Type`**（否则 boundary 丢失，正是 BE-API-008 的坑）。
   已做变异验证：把 `Bearer ${token}` 改成 `token` 后该测试确实失败，不是
   空跑。同样在无 toolchain 时 skip。

### 5b. 覆盖率现状（实测）

| 维度 | 数字 |
|------|------|
| operation | 139 → 139 个方法（0 丢失、0 重名） |
| tag | 27 → 27 个文件 |
| `components.schemas` | 8 → 8 个 `interface` |
| body 类型 | 68 个 JSON body（65 `Record<string, unknown>` + 3 nullable）；4 个 multipart **跳过 body** |
| response 类型 | 129 `Record<string, unknown>`、1 `HealthResponse`、2 具体 `Record<...>`、7 `unknown` |
| `unknown` response | 7 个，全部是 schema 为 `{}` 的流式 / 文件下载端点（SSE `alerts/stream`、`chat/stream`、PDF、ZIP、媒体字节流）—— 本就没有 JSON schema 可推 |

绝大多数 response 是 `Record<string, unknown>` 而非具体 interface，原因是
现存端点大量 `-> dict` 而没有 `response_model=`。这**不是生成器的短板**：
schema 里就只有 `additionalProperties: true`。想要具体类型得先给端点补
`response_model`（可作 BE-API-011），生成器届时自动升级、无需改动。

### 6. 风险 & 缓解

| 风险 | 缓解 |
|------|------|
| 生成物被手工改，下次生成被覆盖 | 每个文件头 `// AUTO-GENERATED … DO NOT EDIT` + 生成命令；`index.ts` 也一样 |
| 138 个方法里有 shape 我们没覆盖（oneOf / allOf） | `ts_type()` 递归带兜底：未知构造 → `unknown`（不是 `any`），编译期强制调用方 narrow |
| multipart 端点（4 个）生成不出可用签名 | 本工单**显式跳过 body**，方法仍生成（path/query 参数齐全）并在 TSDoc 打 `@remarks multipart body not yet generated`；见「后续」 |
| `app.openapi()` 触发 infra 连接（Chroma 等）导致慢/噪音 | `create_app()` 只注册路由，不跑 lifespan；Chroma 连接失败已有 in-memory fallback（warning 不影响 schema） |
| 前端仓库不在本 repo | 输出目录 `frontend/src/api/` 可用 `--out` 覆盖；本 repo 内产出即是「待 vendored 的一份」 |

### 7. 不在本工单

- ❌ multipart / `FormData` body（4 个端点，见后续）
- ❌ SSE / WebSocket 端点的流式客户端（`/alerts/stream/{child_id}`、`/chat/stream` 走原生 `EventSource`，生成器只给 URL builder）
- ❌ React Query / SWR hooks 生成（SDK 是 framework-agnostic 的裸函数）
- ❌ 运行时 schema 校验（zod / valibot）—— 那是 npm 依赖，且 §6.3 已有服务端校验
- ❌ 把生成物 commit 进前端仓库的 CI 流水线（前端仓库自己决定 vendor 还是 build 时生成）

## 后续

- **BE-API-008**：**multipart body 支持**。当前生成器对
  `requestBody.content["multipart/form-data"]` 的 4 个端点
  （`POST /api/v1/media/upload`、`POST /api/v1/media/avatar`、
  `POST /api/v1/media/ocr`、`POST /api/v1/audio/transcribe`）
  只生成 path/query 参数，**不生成 body 参数** —— 方法体带
  `@remarks multipart body not yet generated`，调用方需自己传
  `options.body`。下个工单要：从 `Body_*` schema 推导
  `{ file: Blob | File; language?: string }` 形状的参数、在
  `client.ts` 加 `FormData` 分支（并**不能**手设 `Content-Type`，
  须让浏览器自己填 boundary）、以及 upload progress 的可选回调。
- **BE-API-009**（可选）：SSE 端点的 typed `EventSource` wrapper。
- **BE-API-010**（可选）：生成物 drift 检查 —— CI 里重跑生成器并 `git diff --exit-code`，防止手改生成物。
  （本工单已提供 `make ts-sdk-check` / `--check`，接线到 CI 即可。）

## 顺带发现（不在本工单修）

`tests/api/test_pagination.py::TestMemoryGraphEntitiesPagination::test_explicit_pagination`
是一个**既有的时序 flake**（引入于 BE-API-003，与本工单无关）：

测试连续 `add_entity` 25 个 entity，然后断言按 `first_seen` desc 排序后
`entities[10]["name"] == "e10"`。但 `first_seen` 取 `datetime.now()`，25 次
插入通常落在**同一个时钟 tick** 内 → 全部 timestamp 相等 → Python 的稳定
排序保留插入顺序 → 断言恰好成立。一旦时钟边界落在循环中间（实测
在第 13 个插入后 `sleep(0.03)` 即可复现），排序被真实的 timestamp 分成
两段，`entities[10]` 变成 `e23`，断言失败。

本工单跑全量测试时命中过 1 次（1675 passed / 1 failed），单独跑与后续
3 次全量跑均通过 —— 新增的测试只是改变了执行时机、把这个既有竞态暴露
出来，**不是回归**（`git diff` 对 `test_pagination.py` / `graph.py` /
`_pagination.py` / `memory.py` 均为空）。

正解是给排序加确定性 tie-break（如 `key=(first_seen, name)`）或让测试
不依赖同 tick 假设。留给 BE-API-003 的后续或独立的 flake 清理工单。
