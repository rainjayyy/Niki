# Niki — 问题审计清单

> 审计时间：2026-07-23
> 审计范围：94 个源文件 + 70 个测试文件，全量读 + 全量跑
> 测试结果：775 / 775 通过，但多处真实 bug 被现有测试集漏掉
> 静态检查：ruff lint 全部通过；ruff format 有 73 个测试文件待格式化；mypy 严格模式 72 个错误

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
