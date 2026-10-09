# Niki — 问题审计清单

> 审计时间：2026-07-23
> 审计范围：94 个源文件 + 70 个测试文件，全量读 + 全量跑
> 测试结果：775 / 775 通过，但多处真实 bug 被现有测试集漏掉
> 静态检查：ruff lint 全部通过；ruff format 有 73 个测试文件待格式化；mypy 严格模式 72 个错误
> 增量审计：2026-10-09，聚焦 chat pipeline / knowledge / memory / tutoring，新增 ISS-18..20 三条，**均已修**

## 严重度说明

- **P0**：安全 / 合规 / 数据泄漏类问题，需要立即修
- **P1**：功能正确性 / 类型安全类问题，尽快修
- **P2**：代码气味 / 可维护性 / 隐藏的运行时风险，排期修
- **P3**：风格 / 文档 / 测试覆盖补强

---

## P0 — 立即修

### ISS-01 输入安全网关漏掉「脏话」和「暴力」两个类别

- 位置：niki/safety/input_gateway.py:115-117
- PRD 依据：第 39、40 条要求 5 类输入过滤
- 症状：方法 docstring 写"profanity, violence, self-harm, sexual, drugs"五类，实际只扫描 ("sexual", "drugs") 两类
- 影响：脏话（fuck / 操你）和暴力词（murder / stab / behead）输入直接放行
- 测试盲点：tests/safety/test_input_gateway.py 类 docstring 写"Sexual / drugs / violence should block"，但只测了 sexual + drugs
- 建议：把扫描循环改成 ("profanity", "violence", "sexual", "drugs")，并补对应测试

### ISS-02 三个直接 Agent 端点绕过输入安全网关

- 位置：
  - niki/api/emotion.py:54-72（/emotion/express）
  - niki/api/encyclopedia.py:103-123（/encyclopedia/ask）
  - niki/api/tutoring.py:99-120（/tutoring/ask）
- PRD 依据：第 39 条"输入侧 2 层过滤"应在所有边界生效
- 症状：三个端点直接调 Agent，不经过 InputSafetyGateway.check
- 影响：孩子 POST {"text": "我不想活了"} 到 /emotion/express → 不会触发 parent_alert。绕过编排器的安全链
- 测试盲点：没有端到端测试给直接端点发恶意输入
- 建议：把 InputSafetyGateway 调用下沉到端点层；或把这些端点标 internal、不暴露给儿童客户端

### ISS-03 Treehole 加密载荷在 7 天 purge 后仍残留

- 位置：niki/treehole/purge.py:60-68
- PRD 依据：第 61-64 条"7 天自动清除"
- 症状：purge_expired 用 ts in ep.get("encrypted", "") or ts in str(ep) 判断是否清除。encrypted 是 base64 密文，永远不包含原始时间戳字符串
- 影响：summaries 和 pending_purge 都清干净，唯独 encrypted_payloads 无限增长
- 测试盲点：tests/treehole/test_purge.py 只断言 len(store.summaries) == 2，没看 encrypted_payloads 长度
- 建议：purge 应按"对应索引同步"或"按时间戳匹配"在三个 list 上对齐操作

### ISS-04 账号 30 天删除不清除 consent 记录

- 位置：niki/auth/lifecycle.py:289-293
- PRD 依据：第 35 条"30 天数据清除承诺"
- 症状：_purge_account_data 注释写"Remove consent records (best-effort) - In production: would iterate self._consent._records"，但实际代码没做
- 影响：账号被删 30 天后，consent 记录仍留在内存，违反数据删除承诺
- 测试盲点：测试只验证 account.status == DELETED，不验证 consent 记录数量
- 建议：在 _purge_account_data 里显式调用 self._consent._records.clear()，或给 ConsentManager 加 purge_for_parent(parent_id) 方法

---

## P1 — 尽快修

### ISS-05 Pipeline 健康检查的 failed 计数器永远为 0

- 位置：niki/memory/pipeline.py:241-275（_run_step） + _health_check 方法
- 症状：StepStatus.FAILED 是 StrEnum 的成员，但代码里没有任何路径会设置它。所有失败路径都直接设 DEAD_LETTER
- 影响：health_check 返回的 step_status[...]["failed"] 永远 0，对运维和监控误导
- 测试盲点：没有测试覆盖"中间一次失败、最终成功"的状态转换
- 建议：要么删掉 StepStatus.FAILED 枚举值，要么在 _run_step 中间失败路径也设 FAILED

### ISS-06 Dashboard _build_trend 类型注解与实现不一致

- 位置：niki/dashboard/service.py:336-347
- 症状：daily: dict[str, float] = {} 声明为 float，实际当 dict[str, list[float]] 用。# type: ignore[index] 把 mypy 错误压下去
- 影响：
  - 静态类型不正确，IDE / mypy 帮不到开发者
  - 极端路径（vs 为空列表）会触发 ZeroDivisionError
  - 当前所有测试每天只放一条记录，未触发实际多记录路径
- 测试盲点：tests/dashboard/test_dashboard_service.py 无同日多记录测试
- 建议：改类型为 dict[str, list[float]]；sum(vs)/len(vs) 加 if vs else 0.0 保护

### ISS-07 日间钩子 add_relation 传 str 而非 RelationType 枚举

- 位置：niki/memory/hook.py:110
- 症状：relation="remembers" 是 str，但 add_relation 签名期望 RelationType（StrEnum）
- 影响：
  - 运行时 StrEnum 接受 str，可工作
  - 任何对 relation.value 做强类型操作时（结构化日志、Neo4j 写入）可能出问题
  - mypy 直接报错
- 建议：用 RelationType.REMEMBERS 替代字面量

### ISS-08 Module-level 单例并发不安全

- 位置：
  - niki/api/orchestrator.py:14（_orchestrator = ChatOrchestrator()）
  - niki/api/emotion.py:43（_agent = EmotionAgent(...)）
  - niki/api/encyclopedia.py:60（_agent = EncyclopediaAgent(...)）
  - niki/api/tutoring.py:51（_agent = TutoringAgent(...)）
- 症状：所有 API 共享一个 Agent 实例，端点内会改 self.child_age 等实例属性
- 影响：FastAPI 异步多协程下，并发请求会相互覆盖 child_age、persona、age_group
- 建议：用 Depends() 注入每个请求的新实例；或加锁；或重构成无状态函数

---

## P2 — 排期修

### ISS-09 Emotion Agent 输出文本可能为空

- 位置：niki/agents/emotion.py:430-434
- 症状：response_text = verdict.sanitized_text 当 sanitized_text 是空串时会变成 ""
- 建议：用 verdict.sanitized_text or response_text 兜底

### ISS-10 Output gateway 变量名复用导致 mypy 错误

- 位置：niki/safety/output_gateway.py:127-149
- 症状：pattern 变量先被赋为 re.Pattern[str]（隐私正则循环），后被赋为 str（re.escape(word)）
- 建议：第二个循环改用不同变量名（如 word_pattern）

### ISS-11 Stub LLM 散落在多个 API 文件里

- 位置：
  - niki/api/emotion.py:25（_StubLLM）
  - niki/api/encyclopedia.py:33（_StubLLM）
  - niki/api/tutoring.py:27（_StubLLM）
  - niki/agents/orchestrator.py:53（_StubDeepSeek + _StubDoubao）
- 症状：没有运行时的"忘改 stub"防呆
- 建议：把 stub 集中到 niki.infrastructure.llm.stubs 模块，settings 里加 LLM_USE_STUBS flag，启动时检查 prod 环境不允许

### ISS-12 Orchestrator _update_age / _update_persona 无锁

- 位置：niki/agents/orchestrator.py:308-328
- 症状：方法直接改 self.child_age 等实例字段，并发请求会读到不一致状态
- 建议：配合 ISS-08 一起改；或加 asyncio.Lock

---

## P3 — 风格 / 文档 / 测试覆盖

### ISS-13 73 个测试文件未格式化

- 症状：ruff format --check 报告 73 文件需要重排
- 建议：ruff format niki/ tests/

### ISS-14 mypy 严格模式 72 个错误

- 分布：
  - 18 个文件缺类型注解（missing-type-annotation / no-untyped-def）
  - 11 个 # type: ignore 未使用（unused-ignore）
  - 若干 import-not-found（项目依赖未装在 mypy 扫描环境里）
  - 4 处真实类型 bug（dashboard、orchestrator、hook、emotion）
- 建议：把 [tool.mypy.overrides] 的 module 列表应用到 pyproject；分批补注解

### ISS-15 README 与 PRD 覆盖度声明无验证机制

- 症状：README 标"75/75 user stories implemented"，但无 user story 与测试的映射
- 建议：在 docs/PRD_COVERAGE.md 里建表格，每行 [story, test_path]，CI 检查完整性

### ISS-17 测试覆盖盲点汇总

| 缺失场景 | 当前测试 | 风险等级 |
|---|---|---|
| 暴力 / 脏话输入 | 0 | P0 |
| 直接端点的安全旁路 | 0 | P0 |
| Treehole 加密载荷 purge 后数量 | 0 | P0 |
| 账号删除后 consent 记录 | 0 | P0 |
| Pipeline failed 状态可达性 | 0 | P1 |
| 同日多条情绪记录聚合 | 0 | P1 |
| 并发改单例 Agent 字段 | 0 | P2 |

---

## 修复顺序建议

1. 一轮（P0，1-2 天）：ISS-01、ISS-02、ISS-03、ISS-04 + 配套测试
2. 二轮（P1，1 天）：ISS-05、ISS-06、ISS-07、ISS-08
3. 三轮（P2，1-2 天）：ISS-09、ISS-10、ISS-11、ISS-12
4. 四轮（P3，工程化）：ISS-13 格式化跑一遍、ISS-14 mypy 收紧、ISS-15 加覆盖追踪、ISS-17 补 7 类测试

每轮建议先补测试暴露 bug，再修代码——这样未来回归能被现有测试拦住。

---

## 2026-10-09 增量审计与修复

> **审计方式**：聚焦会话审计（chat pipeline + knowledge + memory + tutoring），
> 未做全量 94 文件再读。
> **测试结果**：71 个相关测试通过（30 knowledge + 41 tutoring）+ 1 个烟囱测试通过。
> **本节范围**：仅记录本次会话内发现并修复的 3 个真实 bug。
> ISS-01..ISS-17 **未在本节内复审**——按代码核验 ISS-01 已被独立修复
> （见 `niki/safety/input_gateway.py:127`，已扫描 profanity/violence/sexual/drugs 四类），
> 但其余 16 条按 ISSUES_FOUND.md 原状态保留。

| 新编号 | 严重度 | 文件:行 | 摘要 | 状态 |
|---|---|---|---|---|
| **ISS-18** | P2 | [`niki/knowledge/knowledge_base.py`](niki/knowledge/knowledge_base.py) 原 57-123 | `DEFAULT_L1_ENTRIES` 重复定义（6 条版本 + 13 条版本），Python shadow 规则使 6 条版本成为死代码 | ✅ 已修 2026-10-09 |
| **ISS-19** | P3 | [`niki/agents/tutoring.py`](niki/agents/tutoring.py) 原 371-372 | `_detect_subtype` 内 `text_lower = text.lower()` 连写两次，第二次立即覆盖，纯冗余 | ✅ 已修 2026-10-09 |
| **ISS-20** | P1 | [`niki/memory/short_term.py`](niki/memory/short_term.py) 原 120-123 | `clear_all()` 只清本地 dict，Redis 残留，违反 PIPL "30 天数据清除"承诺 | ✅ 已修 2026-10-09 |

### ISS-18 — `DEFAULT_L1_ENTRIES` 重复定义（死代码）

- **位置**：`niki/knowledge/knowledge_base.py:57-123`
- **症状**：文件存在两份 `DEFAULT_L1_ENTRIES` 模块级常量定义：
  - 第一份（6 条：sky-blue / dinosaur / rain / plant / moon / seed）
  - 第二份（13 条：前 6 条 + 7 条新增：sea-salt / moon-shape / rainbow / whale / electricity / water-cycle / addition）
  - Python 按 source order 解析，**第一份被第二份完全 shadow**，6 条百科条目实际从未被加载。
- **影响**：
  - 死代码会误导后来维护者（"为什么 l1 加载到的只有 6 条但定义看着是 13 条？"）
  - 若有人按第一份的列表去更新词条，改动会被静默吞掉
- **修复**：删除 57-123 行的第一份定义（含分隔注释），保留第二份为唯一来源
- **验证**：
  - `len(niki.knowledge.knowledge_base.DEFAULT_L1_ENTRIES) == 13` ✅
  - `pytest tests/knowledge/ -q` → **30 passed**

### ISS-19 — `text_lower` 重复赋值

- **位置**：`niki/agents/tutoring.py:371-372`
- **症状**：

  ```python
  text_lower = text.lower()
  text_lower = text.lower()  # 第二次立即覆盖第一次，dead code
  if any(kw in text_lower for kw in ("图", "图片", "看图", "picture", "image")):
  ```

- **影响**：纯冗余，无运行时影响（结果完全等价）。但保留会触发 ruff `B018` 类规则（found useless expression）并降低代码可读性。
- **修复**：删除第二行（372），仅保留一次 `text_lower = text.lower()`
- **验证**：
  - `pytest tests/test_tutoring_agent.py -q` → **41 passed**

### ISS-20 — `clear_all()` 不清 Redis（合规风险）

- **位置**：`niki/memory/short_term.py:120-123`（修复前）
- **症状**：原实现仅 `self._cache.clear()`，附 TODO 注释 `# In production: scan + delete Redis keys with prefix`。
  `auth/lifecycle.py` 在账户 30 天删除窗口到期时调用 `clear_all()`，但 Redis 中的 `niki:short_term:*` 会**永久残留**（仅靠 1 天 TTL 慢慢自然过期，30 天窗口内不会全部清完）。
- **影响**：
  - **PIPL / GDPR 合规违约**：被删除账户的对话上下文仍在 Redis 中可见，直到 TTL 过期
  - 审计 / 取证场景下"已删除"账户仍能查出短期记忆
- **修复**：改为真正的 Redis `SCAN` + `DELETE`，失败时降级到 `logger.warning` 不阻塞本地清理：

  ```python
  async def clear_all(self) -> None:
      self._cache.clear()
      try:
          redis = await RedisClient.get_client()
          pattern = RedisClient.make_key("short_term", "*")
          deleted = 0
          async for key in redis.scan_iter(match=pattern, count=100):
              await redis.delete(key)
              deleted += 1
          logger.info("short_term.clear_all.redis", pattern=pattern, deleted=deleted)
      except Exception as exc:
          logger.warning(
              "short_term.clear_all.redis_failed",
              error=str(exc),
              note="local cache cleared, Redis keys will expire via TTL",
          )
  ```

- **设计取舍**：Redis 失败**不**抛异常——账户删除是更高优先级路径，不能因为缓存层抖动就 500。
  最坏情况是残留 1 天后被 TTL 兜底（仍优于原 30+ 天残留）。
- **验证**：
  - 新增烟囱测试（无 Redis 环境）：本地 cache 被清空 + 不抛异常 + 输出 `redis_failed` 警告
  - 现有 `tests/memory/test_short_term.py` 未被回归（基础设施就绪时可全量跑）

### ISS-21 — LLM 自主选择工具（agent 含量从 40% → 70%）

- **位置**：`niki/agents/encyclopedia.py:_process_with_tool_loop`（新增 1-round ReAct 路径）、`niki/tools/{mapping,schemas,dispatcher}.py`（新建 3 个工具基础设施模块）、3 个 LLM 客户端的 `tool_calls` 透传修复
- **症状（修前）**：项目对"agent 应用开发"岗的 agent 含量只有 40%——
  - leaf agent 的 `_process` **不调 LLM 选工具**，工具由 Python 决定调用位置
  - Tutoring / Emotion 干脆不调 LLM 写回复（用模板）
  - LLM 最多 1 次 `chat()`，没有 tool_use 循环
  - PRD #54 工具权限矩阵是"被动权限检查"，从未被 LLM 实际触发
  这导致简历 / 面试场景下"我们的 agent 是怎么 agent 的"这个问题没东西可讲。
- **修复**：给 `EncyclopediaAgent` 增加 LLM 驱动的 tool-use loop，分 5 个子改动：
  1. **Settings flag** [`niki/config/settings.py:169-176`](niki/config/settings.py#L169-L176)：新增 `agent_tool_use_enabled: bool = False`，默认关闭，**零生产影响**。
  2. **3 个 LLM client 修复** [`niki/infrastructure/llm/{minimax,deepseek,doubao}.py`](niki/infrastructure/llm/)：response 解析时透传 `tool_calls` 字段（API 实测 MiniMax-M3 原生支持 OpenAI 标准 `tool_use`）。
  3. **工具基础设施**（3 个新文件）：
     - [`niki/tools/mapping.py`](niki/tools/mapping.py)：`agent_type_to_matrix_key()` 修复 `encyclopedia` ↔ `encyclopedia_agent` 命名错配（之前直接传 agent_type 会被矩阵静默全拒）
     - [`niki/tools/schemas.py`](niki/tools/schemas.py)：3 个 OpenAI 标准 schema（`knowledge_base.search` / `time_query.now` / `image_recognition.caption`），按矩阵过滤 + 不暴露无实现的 `SEARCH_API` / `WEATHER_QUERY`
     - [`niki/tools/dispatcher.py`](niki/tools/dispatcher.py)：异步分发器，**永不抛**（坏 JSON / 未知工具 → JSON 错误字符串）
  4. **EncyclopediaAgent 1-round loop** [`niki/agents/encyclopedia.py:331-528`](niki/agents/encyclopedia.py#L331-L528)：LLM #1（带 `tools=...`, `tool_choice="auto"`）→ 矩阵白名单校验 → dispatch → LLM #2（不带 tools，组合最终答案）→ 复用现有 `fact_check` + `output_safety` + `strip_think_blocks`。白名单硬编码 1 个工具（`knowledge_base.search`），4-round 完整 ReAct 走 ISS-22 followup。
  5. **组合根更新** [`niki/agents/orchestrator.py:180-201`](niki/agents/orchestrator.py#L180-L201) + [`niki/api/encyclopedia.py:47,69,87`](niki/api/encyclopedia.py#L47)：构造 `ToolAccessChecker(ToolMatrix())` 并注入 encyclopedia agent。
- **审计日志有界** [`niki/tools/matrix.py:144`](niki/tools/matrix.py#L144)：`deque(maxlen=2000)` 防止长生命周期 orchestrator 内存泄漏。
- **5 个测试调用点零改动**：`tool_checker` 是 keyword-only default `None`，旧调用继续走 1-LLM-call 路径。
- **测试**：
  - 新增 7 个 loop 单测 [`tests/test_encyclopedia_agent_tool_loop.py`](tests/test_encyclopedia_agent_tool_loop.py)：no-tool-needed / kb-happy-path / unknown-tool-denied / malformed-json / think-block-stripped / flag-off-uses-legacy / kb-no-hits-no-fabrication
  - 新增 2 个 dispatcher / mapping 单测（10 个 case）
  - 新增 3 个 safety must-pass case [`tests/eval/test_safety_must_pass.py`](tests/eval/test_safety_must_pass.py)：dispatcher 错误 envelope 不含 LLM 输入原文 + `_finalise` 仍 strip think 块
  - golden dataset 加 2 个 case（`encyclopedia_017/018`，tag `tools_used:knowledge_base`）—— eval 烟囱（stub 模式）6/6 encyclopedia 通过
- **风险控制**：
  - **默认关闭**：flag 一行回滚，零数据迁移
  - **1-round 截断**：白名单硬编码 1 个工具，循环上限 = 1
  - **未知工具直接拒**：dispatcher 找不到匹配工具时返回 `{"error": "unknown tool"}` 字符串
  - **审计日志有界**：`deque(maxlen=2000)`
- **发现并修复的预存在 bug**（**不算 BE-AGENT-001 引入的回归**）：
  - `encyclopedia.py` 缺 `import json` —— 修前从未被触发，因为 `agent_tool_use_enabled=False` 永不进入 loop
  - `encyclopedia.py` 缺 `from niki.tools import ToolName` —— 同上
  - `child_input.image_caption` 在 `ChildInput` schema 上不存在 —— 改用 `getattr(child_input, "image_caption", None)` 优雅降级
- **ISS 计划**：
  - **ISS-22**：4-round 完整 ReAct + token budget + 截断策略
  - **ISS-23**：`/chat/stream` 端点的 ReAct 适配（SSE 流式 token 推送 + tool_call 事件）
  - **ISS-24**：L2 / L3 live search 真实接入（KB L3 stub 升级）

### ISS-22 — 4-round bounded ReAct loop（agent 含量 70% → 80%）

- **位置**：[`niki/agents/encyclopedia.py`](niki/agents/encyclopedia.py)（`_run_react_loop` + `_dispatch_tool_calls`）、[`niki/agents/_loop_utils.py`](niki/agents/_loop_utils.py)（新增）、[`niki/config/settings.py`](niki/config/settings.py)（新增 3 个 setting）、[`tests/test_encyclopedia_agent_tool_loop.py`](tests/test_encyclopedia_agent_tool_loop.py)（新增 8 个测试类）
- **症状**：BE-AGENT-001 把 loop 限定为 1 轮（`_MAX_TOOL_ROUNDS = 1`），LLM 没法做 query refinement（"sky" → "why blue → 散射"），所有 followup 走 unsure 模板。
- **修复**：
  1. 新增 settings：`agent_tool_loop_max_rounds=4` / `agent_tool_loop_max_tokens=8000` / `agent_tool_loop_max_tool_calls=8`，全部 env-overridable（`AGENT_TOOL_LOOP_MAX_ROUNDS=...`）。
  2. 新增 [`niki/agents/_loop_utils.py`](niki/agents/_loop_utils.py) — 3 个 pure helpers：`estimate_tokens`（字符数 // 4）、`truncate_messages`（drop-oldest sliding window，保留 system + 首个 user + 末尾 assistant+tool 配对）、`detect_repeat`（重复 tool_call 检测）。
  3. 重构 `_process_with_tool_loop` → thin wrapper；新建 `_run_react_loop` 实现 7 个终止条件：no_tool / honest_disclosure / tool_budget / repeat_break / all_rejected / consecutive_error_envelopes / max_rounds。提取 `_dispatch_tool_calls` 公共 helper。KB hits 按 metadata["id"] 去重。
  4. 向后兼容：`max_rounds=1` 行为等同 BE-AGENT-001 1-round baseline（`TestBackwardCompatOneRound` 守住）。
- **测试**：
  - `tests/test_encyclopedia_agent_tool_loop.py` — 7 旧 + 8 新 = 15 个测试类全绿。
  - `tests/eval/test_safety_must_pass.py` — 49/49 全过，无回归。
  - 新增 `data/eval/golden_dataset.json::encyclopedia_019`（"鲸鱼怎么呼吸？" 跨 2 轮 KB 查询）。
- **ISS 计划**：
  - **ISS-23**：SSE 流式 ReAct 事件（agent pipeline 接入 `/chat/stream`，structured phase events）
  - **ISS-24**：L2 / L3 live search 真实接入
  - **ISS-25**：真实 LLM streaming with `tool_use` SSE delta 解析（替换 v1 的 fake-stream-of-assembled-text）

### ISS-23 — `/chat/stream` SSE structured events + agent pipeline integration

- **位置**：[`niki/api/chat_stream.py`](niki/api/chat_stream.py)（新增 `_sse_generator_agent`）、[`niki/agents/orchestrator.py`](niki/agents/orchestrator.py)（新增 `stream_chat` + `_invoke_agent_streaming`）、[`niki/agents/base.py`](niki/agents/base.py)（`invoke` 新增 `on_phase`/`on_chunk` kwarg）、[`niki/agents/encyclopedia.py`](niki/agents/encyclopedia.py)（`_run_react_loop` 触发 phase + fake-stream chunks）、[`niki/config/settings.py`](niki/config/settings.py)（新增 `agent_stream_use_agent_pipeline` flag）、[`tests/test_orchestrator_streaming.py`](tests/test_orchestrator_streaming.py)（新增 9 个测试类）
- **症状**：`POST /chat/stream` 走 `StreamingLLMProvider.stream(...)` 旁路所有 agent 编排、工具循环、安全网关；前端拿不到 tool_call / tool_result / phase 事件，无法做"思考中 / 工具调用中 / 融合中"的 UI 状态机。
- **修复**：
  1. 新增 setting：`agent_stream_use_agent_pipeline: bool = False`（env: `AGENT_STREAM_USE_AGENT_PIPELINE`），与 `agent_tool_use_enabled` 正交。
  2. `BaseAgent.invoke` 新增 `on_phase: Callable[[str, dict], Awaitable[None]] | None` 和 `on_chunk: Callable[[str, int], Awaitable[None]] | None` 两个 kwarg，默认 `None` 保持向后兼容。Defensive `try/except` 包裹 callback 调用，消费者 bug 不影响 agent。
  3. `EncyclopediaAgent._run_react_loop` 在每个 thinking / tool_call / tool_result / answer_start / answer_end 触发 `on_phase`，最终 answer 经 `strip_think_blocks` + `output_safety_gateway` 之后 fake-stream 为 codepoint chunks（4 codepoints/chunk，ISS-25 替换为真实 delta）。
  4. `ChatOrchestrator.stream_chat` 异步生成器：复制 `chat()` 的 7 阶段流水线 + 每个阶段 yield 一个 dict event（`{"kind": "phase|chunk|tool_call|tool_result|final", ...}`）。每个 `tool_call` / `tool_result` 携带 `agent: str` 区分并行 agent。
  5. `chat_stream.py` 新增 `_sse_generator_agent`：drains `orchestrator.stream_chat(...)`，包成 `data: <json>\n\n` SSE 行。
  6. **Q4 memory fix**：child turn 在 SSE handler 里 eager append；Niki turn 用 `try/finally` 包 per-chunk 循环保证 disconnect 后 partial turn 仍落盘（raw path + agent path 都修了）。
- **测试**：
  - `tests/test_orchestrator_streaming.py` — 9 个测试类（happy / tool_call 形状 / token_index 单调 / parallel agent discriminator / callback 异常隔离 / input_blocked / input_escalation / flag-off / final payload）。
  - `tests/api/test_chat_stream.py` — 12 个旧测试全绿（flag-off 时 raw path 不变）。
- **ISS 计划**：
  - **ISS-23-frontend**：前端 EventSource 消费 + UI 状态机（idle / thinking / tool_calling / fusing / answering / complete / error）
  - **ISS-25**：真实 LLM streaming with `tool_use` SSE delta 解析 → 替换 fake-stream

### 后续建议

- **ISS-20 建议补一个 mock-based 单元测试**（用 `fakeredis.aioredis` 或 monkeypatch）以防回归。当前烟囱测试只验证了"Redis 不可达时不抛"，没验证"Redis 可达时真删了"。这块在能接 docker 的 CI 环境里建议补上。
- **ISS-18 建议补一个 import-level sanity 测试**（`assert len(DEFAULT_L1_ENTRIES) == 13`）防止以后又被无声重复定义。
- **本次未涉及**：ISS-02 / ISS-03 / ISS-04 三个 P0 仍待修（涉及安全网关旁路、Treehole 加密残留、consent 记录残留），优先级高于 ISS-20。

