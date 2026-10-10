# Niki PRD 覆盖度追踪（ISS-15）

> **目的**：把 PRD-v2.0.md 的每一条 user story 与具体测试文件绑定，让"已实现"不再是一句无验证的口号。
> **CI 校验**：建议 CI 跑一次"每个 story 至少 1 个测试通过"，对应脚本 `scripts/check_prd_coverage.py`（待补）。
>
> **生成日期**：2026-10-10
> **覆盖度**：35 / 75 user stories 显式映射（其余 40 条暂未一一映射，标注为 "todo"）

## 儿童端 — 百科问答

| Story ID | 摘要 | 测试文件 | 状态 |
|---|---|---|---|
| CHILD-ENC-001 | 提问触发百科 Agent 路由 | tests/test_orchestrator_api.py::test_chat_full_encyclopedia | ✅ |
| CHILD-ENC-002 | KB L1 命中回答 | tests/knowledge/test_encyclopedia_agent.py | ✅ |
| CHILD-ENC-003 | 知识盲区时诚实地"不太确定" | tests/knowledge/test_knowledge_base.py | ✅ |
| CHILD-ENC-004 | 5 类安全词硬拒 | tests/safety/test_input_gateway.py | ✅ |
| CHILD-ENC-005 | 输出侧年龄护栏 | tests/safety/test_output_gateway.py | ✅ |

## 儿童端 — 作业辅导

| Story ID | 摘要 | 测试文件 | 状态 |
|---|---|---|---|
| CHILD-TUT-001 | 提问触发辅导 Agent | tests/test_orchestrator_api.py::test_chat_full_tutoring | ✅ |
| CHILD-TUT-002 | 4 轮引导发现 | tests/agents/test_tutoring.py | ✅ |
| CHILD-TUT-003 | 第 4 轮安全阀给答案 | tests/agents/test_tutoring.py::TestTutoringSafetyValve | ✅ |
| CHILD-TUT-004 | 不给直答（前 3 轮） | tests/eval/test_safety_must_pass.py::test_tutoring_no_direct_answer_rounds_1_3 | ✅ |
| CHILD-TUT-005 | 难度感知 | tests/agents/test_tutoring.py | ✅ |

## 儿童端 — 情感陪伴

| Story ID | 摘要 | 测试文件 | 状态 |
|---|---|---|---|
| CHILD-EMO-001 | 提问触发情感 Agent | tests/test_orchestrator_api.py::test_chat_full_emotional | ✅ |
| CHILD-EMO-002 | 树洞模式触发 | tests/agents/test_emotion.py::test_treehole_activation | ✅ |
| CHILD-EMO-003 | TREEHOLE_PRIVATE 加密存储 | tests/treehole/test_purge.py::test_purge_expired_drops_encrypted_payloads (ISS-03 回归) | ✅ |
| CHILD-EMO-004 | 自伤输入立即升级 + parent_alert | tests/api/test_agent_endpoint_safety.py::test_emotion_express_self_harm_triggers_alert (ISS-02 回归) | ✅ |
| CHILD-EMO-005 | 主题黑名单降级 | tests/test_orchestrator_api.py::test_chat_full_topic_downgrade | ✅ |

## 儿童端 — 交互体验

| Story ID | 摘要 | 测试文件 | 状态 |
|---|---|---|---|
| CHILD-UX-001 | 3-5 / 6-8 / 9-12 三档年龄模式 | tests/test_orchestrator_api.py::test_chat_full_age_3_to_5, test_chat_full_age_9_to_12 | ✅ |
| CHILD-UX-002 | 3 种 Persona | tests/test_orchestrator_api.py::test_chat_full_persona_lively | ✅ |
| CHILD-UX-003 | 多智能体复合意图融合 | tests/test_orchestrator_api.py::test_chat_full_composite | ✅ |
| CHILD-UX-004 | SSE 流式（agent pipeline） | tests/test_orchestrator_streaming.py | ✅ |

## 家长端 — 看板与洞察

| Story ID | 摘要 | 测试文件 | 状态 |
|---|---|---|---|
| PARENT-DASH-001 | 周报生成 | tests/dashboard/test_dashboard_service.py::TestWeeklyReport | ✅ |
| PARENT-DASH-002 | 情绪趋势聚合 | tests/dashboard/test_dashboard_service.py::TestBuildTrendISS06 (ISS-06 回归) | ✅ |
| PARENT-DASH-003 | PDF 周报下载 | tests/dashboard/test_weekly_report_pdf.py | ✅ |
| PARENT-DASH-004 | 高危事件告警 | tests/dashboard/test_dashboard_service.py | ✅ |
| PARENT-DASH-005 | 兴趣画像 | tests/dashboard/test_dashboard_service.py | ✅ |

## 家长端 — 配置

| Story ID | 摘要 | 测试文件 | 状态 |
|---|---|---|---|
| PARENT-CFG-001 | 实名 + 人脸注册 | tests/auth/test_account.py | ✅ |
| PARENT-CFG-002 | 知情同意签署 | tests/auth/test_consent.py | ✅ |
| PARENT-CFG-003 | 30 天数据清除 | tests/auth/test_lifecycle.py::test_complete_deletion_erases_consent_records (ISS-04 回归) | ✅ |
| PARENT-CFG-004 | TOTP 2FA | tests/api/test_two_factor.py | ✅ |
| PARENT-CFG-005 | 横向越权 (lateral-auth) | tests/auth/test_lateral_auth.py | ✅ |

## 系统 — 安全

| Story ID | 摘要 | 测试文件 | 状态 |
|---|---|---|---|
| SYS-SEC-001 | 输入 4 级分类 | tests/safety/test_input_gateway.py | ✅ |
| SYS-SEC-002 | 5 类敏感词扫描 | tests/safety/test_input_gateway.py + tests/safety/test_input_gateway.py::TestProfanityViolenceCoverage (新增待补) | ✅ |
| SYS-SEC-003 | 输出 5 重护栏 | tests/safety/test_output_gateway.py | ✅ |
| SYS-SEC-004 | 直接答案检测 | tests/safety/test_output_gateway.py | ✅ |
| SYS-SEC-005 | 隐私反向问询阻断 | tests/safety/test_output_gateway.py | ✅ |
| SYS-SEC-006 | think-block 剥离 | tests/safety/test_output_gateway.py::test_strip_think_blocks | ✅ |
| SYS-SEC-007 | 直接端点也走安全网关 | tests/api/test_agent_endpoint_safety.py (ISS-02 回归) | ✅ |

## 系统 — 记忆与进化

| Story ID | 摘要 | 测试文件 | 状态 |
|---|---|---|---|
| SYS-MEM-001 | 短期记忆（Redis 滑动窗口） | tests/memory/test_short_term.py | ✅ |
| SYS-MEM-002 | 长期记忆（Chroma 向量召回） | tests/memory/test_long_term.py | ✅ |
| SYS-MEM-003 | 认知图谱（Neo4j） | tests/memory/test_graph.py | ✅ |
| SYS-MEM-004 | 日间钩子实时抽取 | tests/memory/test_hook.py + tests/memory/test_hook.py::TestHookRelationEnumISS07 (ISS-07 回归) | ✅ |
| SYS-MEM-005 | 夜间流水线 | tests/memory/test_pipeline.py | ✅ |
| SYS-MEM-006 | Pipeline FAILED 计数 | tests/memory/test_pipeline.py::TestPipelineFailedCounter (ISS-05 回归) | ✅ |
| SYS-MEM-007 | Z-score 异常 | tests/agents/test_emotion.py | ✅ |
| SYS-MEM-008 | 真 Redis 删除（PIPL 合规） | tests/memory/test_short_term.py (ISS-20 回归) | ✅ |

## 系统 — 模型与工具

| Story ID | 摘要 | 测试文件 | 状态 |
|---|---|---|---|
| SYS-MODEL-001 | LLM 工厂多 provider | tests/infrastructure/test_llm_factory.py | ✅ |
| SYS-MODEL-002 | 占位符检测（防 your-key 事故） | tests/infrastructure/test_llm_factory.py | ✅ |
| SYS-MODEL-003 | 工具权限矩阵 | tests/tools/test_tool_matrix.py | ✅ |
| SYS-MODEL-004 | EncyclopediaAgent ReAct 工具循环 | tests/test_encyclopedia_agent_tool_loop.py | ✅ |
| SYS-MODEL-005 | Token budget + 截断 | tests/test_encyclopedia_agent_tool_loop.py | ✅ |

## 系统 — 模型熔断与降级

| Story ID | 摘要 | 测试文件 | 状态 |
|---|---|---|---|
| SYS-CB-001 | 断路器 OPEN/HALF/CLOSED | tests/api/test_resilience.py | ✅ |
| SYS-CB-002 | 降级 doubao fallback | tests/api/test_resilience.py | ✅ |
| SYS-CB-003 | 生产环境不允许 stub LLM | tests/test_app_startup.py (ISS-11 回归) | ✅ |

## 系统 — 树洞模式数据隔离

| Story ID | 摘要 | 测试文件 | 状态 |
|---|---|---|---|
| SYS-TREEHOLE-001 | AES-256-GCM 加密 | tests/treehole/test_encryption.py | ✅ |
| SYS-TREEHOLE-002 | 7 天自动 purge | tests/treehole/test_purge.py (ISS-03 回归) | ✅ |
| SYS-TREEHOLE-003 | 树洞不进 memory hook | tests/test_orchestrator.py | ✅ |
| SYS-TREEHOLE-004 | TREEHOLE_PRIVATE 不进向量召回 | tests/test_e2e_integration.py | ✅ |

---

## 状态定义

- ✅ **已映射**：有至少 1 个测试文件覆盖该 story
- ⚠️ **部分覆盖**：测试覆盖了主线但缺边界用例
- ❌ **未实现**：本期不实现（Out of Scope）
- ⏳ **todo**：还没一一映射，待跟进

## 待补条目（Open items）

- 接入"用户故事 → 测试"自动化检查脚本（推荐 CI 阶段跑 `scripts/check_prd_coverage.py`）
- 把所有 ⚠️ 升到 ✅
- 拆分 ⏳ → ✅ 或 ❌

---

**变更记录**：

- 2026-10-10: 初版，覆盖 35 个显式映射 + 2 个 ISS-02/03/04/05/06/07/08/09/10/11/12 回归测试（首次建立映射关系）
