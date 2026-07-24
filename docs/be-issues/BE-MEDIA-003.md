# BE-MEDIA-003：TTS 合成端点（mock provider）

| 字段 | 值 |
|------|-----|
| **优先级** | 🟡 P1 |
| **域** | MEDIA |
| **来源** | `docs/frontend-integration.md` §7.2.4（"音色无法定制"行） |
| **工作量** | 1-2 天 |
| **状态** | 🚧 进行中（2026-07-24） |
| **依赖** | BE-MEDIA-002 ✅（结构对称） |
| **后续** | BE-MEDIA-003-real 接火山 / 讯飞 |

## 背景

§6.4 跨段集成测试中，"儿童端 ↔ Niki 对话"路径需要：

```
文本：Niki 听到你说的话了
  ↓
[audio/wav]
  ↓
扬声器
```

当前唯一路径是浏览器原生 SpeechSynthesis：
- ❌ Safari/iOS 上不可用 / 中文旁白音色差
- ❌ 无法选音色、无法调速
- ❌ 无法离线 work

后端 TTS 端点是行业标准方案。本工单端到端跑通 PoC，real provider 后续接入。

## 验收标准（AC）

- [ ] AC-1：`TTSProvider` ABC + `MockTTSProvider` 默认实现
- [ ] AC-2：`POST /api/v1/audio/synthesize` 接受 JSON `{text, voice, speed, language}`
- [ ] AC-3：mock provider 生成 deterministic 音频（相同 text + voice 永远得到相同音频字节）
- [ ] AC-4：响应 `{audio_base64, mime_type, duration_ms, voice, text_hash, size_bytes}`
- [ ] AC-5：限制 text ≤ 1000 字符；空文本 400；过长 400
- [ ] AC-6：错误码 `EMPTY_TEXT` / `TEXT_TOO_LONG` / `TTS_UNAVAILABLE`
- [ ] AC-7：单测 + 集成测试 ≥ 8 个
- [ ] AC-8：现有 912 测试**零回归**

## 设计

### 1. 端点

```
POST /api/v1/audio/synthesize
Content-Type: application/json

{ "text": "Niki 听到你说的话了", "voice": "child_friendly", "speed": 1.0, "language": "zh-CN" }

→ 200 {
    "audio_base64": "UklGRi..."     ← raw wav bytes
    "mime_type": "audio/wav",
    "duration_ms": 4321,
    "voice": "child_friendly",
    "text_hash": "a1b2c3...",
    "size_bytes": 86422
  }

→ 400 { "error": "EMPTY_TEXT" | "TEXT_TOO_LONG" }
→ 503 { "error": "TTS_UNAVAILABLE" }
```

### 2. Provider 抽象

```python
class TTSProvider(ABC):
    @abstractmethod
    async def synthesize(
        self,
        text: str,
        *,
        voice: str = "child_friendly",
        speed: float = 1.0,
        language: str = "zh-CN",
    ) -> TTSResult:
        ...

@dataclass(frozen=True)
class TTSResult:
    audio: bytes           # raw wav/mp3
    mime_type: str         # "audio/wav"
    duration_ms: int
```

### 3. Mock 行为

Mock 生成**合法 WAV** —— 44 字节 header + silence PCM（0x80 bytes for unsigned 8-bit mono 8kHz）。长这样：

```
RIFF<size>WAVEfmt <fmt-chunk> data<silence>
```

duration_ms 按 text 字符数推算（每字符 ~150ms 一句广告语速）。

前端拿到 base64 可以直接 `<audio src="data:audio/wav;base64,UklGRi...">` 播放。监听会没有内容但**结构正确** —— 真实 provider 替换时只要换实现。

### 4. 触发流程

```
[Niki 响应: "Niki 听到你说的话了"]
  ↓
POST /audio/synthesize { text: "Niki 听到你说的话了", voice: "child_friendly" }
  ↓
[TTS provider (mock or 火山/讯飞)]
  ↓
base64 audio bytes
  ↓
<audio> 元素播放
```

### 5. 风险

| 风险 | 缓解 |
|------|------|
| base64 二进制膨胀 33% | 全局限制 text ≤ 1000 字符，控制产出 ≤ 200KB |
| Mock silence 用户听不到东西会困惑 | dev 模式回 wave header 让前端可以确认 shape 正确 |
| Real provider 慢（>10s） | 异步 + 超时；前端显示"播放中..."loading |
| 体积太大撑爆响应 | 设 size_bytes 上限 5MB |

### 6. 不在本工单

- ❌ 接火山 / 讯飞（→ BE-MEDIA-003-real）
- ❌ 音色库配置 UI（→ 家长后台 BE-DASHBOARD-002 P0）
- ❌ TTS streaming（流式输出是 BE-RT-001 的范畴）

## 后续工单

- **BE-MEDIA-003-real**：火山 TTS / 讯飞 provider
- **BE-RT-001**：WebSocket 流式 LLM 输出（不只是 TTS）
- **BE-MEDIA-004**：音频 OSS 二级缓存（避免重复合成）
