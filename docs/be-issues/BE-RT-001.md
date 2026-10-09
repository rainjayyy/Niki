# BE-RT-001：流式聊天输出（SSE 端点）

| 字段 | 值 |
|------|-----|
| **优先级** | 🟡 P1 |
| **域** | RT (Realtime) |
| **来源** | `docs/frontend-integration.md` §7.2.7 / §7.4 |
| **工作量** | 2 天 |
| **状态** | 🚧 进行中（2026-07-24） |
| **依赖** | BE-MEDIA-003 ✅（与本工单无直接依赖，但接 TTS 形成闭环） |
| **后续** | BE-RT-002 WebSocket alerts（高危告警 stream） |

## 背景

§7.2.7 已经写明：

> **BE-RT-001** | WebSocket 或 SSE 实时通道 | 端点不存在；`POST /chat/full` 是同步阻塞 | 高危告警延迟无法 < 5s；聊天流式输出做不到

实际影响：
- **儿童端 UX 差**：点完说话按钮 → 看到空白 5 秒 → 一整段 Niki 回复吐出来
- **家长端告警延迟**：高危告警要等下一次轮询 /dashboard/alerts
- **前端无法做"打字机"动画**

§7 ticket 推荐 **SSE 先行**（比 WS 简单），本工单按推荐路径实施。

## 验收标准（AC）

- [ ] AC-1：`StreamingLLMProvider` ABC + `MockStreamingProvider` 默认实现
- [ ] AC-2：`POST /api/v1/chat/stream` 接受 `{text, session_id, child_age, persona}` 返回 SSE
- [ ] AC-3：响应为 Server-Sent Events 格式：`Content-Type: text/event-stream`，data: {...} 分块
- [ ] AC-4：每个 data: 是 `{chunk: "..."}` 直到 `[DONE]` 哨兵
- [ ] AC-5：mock provider 每秒 yield 5 个词（~50 ms 间隔），可配置
- [ ] AC-6：空 text 400；负 session_id 400
- [ ] AC-7：流式响应 200 + Content-Type 正确（integration test）
- [ ] AC-8：单测 + 集成测试 ≥ 8 个
- [ ] AC-9：现有 927 passed 测试**零回归**

## 设计

### 1. 端点

```
POST /api/v1/chat/stream
Accept: text/event-stream
Content-Type: application/json

{ "text": "恐龙为什么会灭绝？", "session_id": "abc", "child_age": 7, "persona": "lively" }

→ 200 OK
   Content-Type: text/event-stream
   Cache-Control: no-cache
   Connection: keep-alive

data: {"chunk": "因为", "token_index": 0}

data: {"chunk": "6500万年前", "token_index": 1}

data: {"chunk": "有一颗", "token_index": 2}

...

data: {"chunk": "灭绝了。", "token_index": 17}

data: {"session_id": "abc", "total_text": "...", "is_final": true}
data: [DONE]
```

### 2. Provider 抽象

```python
class StreamingLLMProvider(ABC):
    @abstractmethod
    async def stream(
        self,
        prompt: str,
        **kwargs,
    ) -> AsyncIterator[str]:
        """Yield token chunks one at a time."""

@dataclass
class MockStreamingProvider(StreamingLLMProvider):
    """Yields deterministic reply text word-by-word with small delays."""
```

### 3. 测试 client

SSE client 在 httpx / requests 都支持。用 `client.stream()` + `iter_lines()` 累加 data: 块：

```python
with client.stream("POST", "/chat/stream", json={...}) as resp:
    assert resp.headers["content-type"] == "text/event-stream"
    for line in resp.iter_lines():
        if line.startswith("data: "):
            ...
```

### 4. 风险

| 风险 | 缓解 |
|------|------|
| SSE 长连接占 worker | 设置 generator 退出 timeout；FastAPI 默认会处理 |
| ASGI 服务器要支持 streaming | 使用 `StreamingResponse` 而非 `JSONResponse` |
| 客户端断连后 server 还在 stream | 用 `request.is_disconnected()` 监听 cancellation |
| Real provider 不支持 streaming | mock 实现；real provider followup |

### 5. 不在本工单

- ❌ WebSocket alerts stream（→ BE-RT-002）
- ❌ 真正的 LLM streaming（doubao/deepseek SDK streaming 适配）
- ❌ TTS-streaming（每段 Niki 文本立刻合成 — 复杂）

## 后续工单

- **BE-RT-002**：WebSocket 高危告警推送（家长端实时收到）
- **BE-RT-001-real**：接入 DeepSeek-V4 streaming mode
- **BE-RT-003**：流式 TTS，每段 Niki 输出立刻合成音频

---

## ISS-23 修订（2026-10）

### AC-10 — Agent pipeline 模式 emit phase 事件

当 `agent_stream_use_agent_pipeline=True` 时，`/chat/stream` 应输出
如下结构的 SSE 事件流（payload key 互斥、closed-enum）：

  * `{"phase": "<name>", "payload": {...}}` — `name ∈ {input_safety_ok,
    input_blocked, input_escalation, routed, agent_start, agent_end,
    thinking_start, thinking_end, tool_call, tool_result, answer_start,
    answer_end, fused, output_safety_ok, output_safety_block}`
  * `{"chunk": "<text>", "token_index": <int>}` — 最终回答的 codepoint chunks
  * `{"tool_call": {"agent": "...", "name": "...", "args": {...}}}`
  * `{"tool_result": {"agent": "...", "name": "...", "ok": <bool>, "hit_count": <int>}}`
  * `{"is_final": true, "total_text": "...", ...}` — 终止事件
  * `[DONE]` — sentinel

每个 `tool_*` / `agent_start` / `agent_end` 事件 payload 必须带
`agent: str` 区分并行 agent。Wire format 通过
`tests/test_orchestrator_streaming.py` 9 个测试类 + `tests/api/test_chat_stream.py`
12 个测试（flag-off 时 raw path 不变）共同守护。

### AC-11 — 断连时记忆完整性

child turn 在 SSE handler 里 eager append；Niki turn 用 `try/finally`
包 per-chunk 循环保证 disconnect 后 partial turn 仍落盘。`try/finally`
（**不是** `try/except`）以保留 `asyncio.CancelledError` 传播。
