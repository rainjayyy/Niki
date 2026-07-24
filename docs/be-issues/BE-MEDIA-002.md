# BE-MEDIA-002：音频识别端点（mock ASR provider）

| 字段 | 值 |
|------|-----|
| **优先级** | 🟡 P1 |
| **域** | MEDIA |
| **来源** | `docs/frontend-integration.md` §7.2.4 + §3.2 |
| **工作量** | 2 天 |
| **状态** | 🚧 进行中（2026-07-24） |
| **依赖** | 无 |
| **后续** | BE-MEDIA-002-real 接阿里云一句话识别 |

## 背景

§3.2 已经明确指出：

> ⚠️ **后端当前未实现**：`/api/v1/audio/transcribe` 端点不存在，ASR 集成在 P1 缺口清单（§7.2.4）。

实际情况：
- 儿童端 H5 录音没有 backend 接口可以发
- 端到端"儿童开口 → Niki 回复 → TTS 回放"流程断了
- 唯一一个 demo 路径是浏览器原生 Web Speech，但它在 Safari/iOS 上不可用

本工单在 PoC mock provider 下端到端跑通，real provider 留给后续工单。

## 验收标准（AC）

- [ ] AC-1：`ASRProvider` ABC + `MockASRProvider` 默认实现
- [ ] AC-2：`POST /api/v1/audio/transcribe` 接受 `multipart/form-data`，字段 `audio` (file)、可选 `language` (str)、可选 `child_age` (int)
- [ ] AC-3：mock provider 基于音频字节 hash 返回 deterministic text（相同 hash 总是得到相同 text）
- [ ] AC-4：响应格式 `{text, duration_ms, confidence, language, audio_hash}`
- [ ] AC-5：大小限制 ≤ 5 MB；空文件 400；超限 413；不支持格式 400
- [ ] AC-6：错误码 `EMPTY_AUDIO` / `AUDIO_TOO_LARGE` / `UNSUPPORTED_FORMAT` / `ASR_UNAVAILABLE`
- [ ] AC-7：单测 + 集成测试 ≥ 8 个
- [ ] AC-8：现有 901 测试**零回归**

## 设计

### 1. 端点

```
POST /api/v1/audio/transcribe
Content-Type: multipart/form-data; boundary=...

audio=@speech.wav              # required, <= 5MB, wav/m4a/opus/mp3
language=zh-CN                 # optional, default "zh-CN"
child_age=7                    # optional, helps kids-speech accuracy

→ 200 {
    "text": "Niki 你好",
    "duration_ms": 1234,
    "confidence": 0.92,
    "language": "zh-CN",
    "audio_hash": "a1b2c3d4..."   # hex sha1 of bytes (for traceability)
  }

→ 400 { "error": "EMPTY_AUDIO" | "UNSUPPORTED_FORMAT" }
→ 413 { "error": "AUDIO_TOO_LARGE" }
→ 503 { "error": "ASR_UNAVAILABLE" }
```

### 2. 触发流程

```
[前端 Web 录音]
  1. MediaRecorder 采集 audioBlob (webm/opus)
  2. POST /audio/transcribe multipart
     ↓
[后端]
  3. validate size + format
  4. read bytes
  5. provider.transcribe(audio_bytes, language, child_age)
  6. return {text, duration_ms, confidence, language, audio_hash}
     ↓
[前端]
  7. text → /chat/full → 走到 Niki → 响应 → TTS
```

### 3. ASRProvider 抽象

```python
class ASRProvider(ABC):
    @abstractmethod
    async def transcribe(
        self,
        audio: bytes,
        *,
        language: str = "zh-CN",
        child_age: int | None = None,
    ) -> ASRResult:
        """Decode audio and return text + meta."""

@dataclass
class ASRResult:
    text: str
    duration_ms: int
    confidence: float
    language: str
```

### 4. Mock 行为

Mock 不真的读音频字节（不上 librosa/audioop），而是：
- 用 SHA-1(audio_bytes)[:8] 派生 deterministic 文本
- duration_ms 取 hash 数值后两位 ÷ 1000（伪随机但 deterministic）
- confidence 固定 0.92

这样测试可重复，且 dev demo 能展示"上传了 audio，Niki 收到了文本"的完整链路。

### 5. 风险

| 风险 | 缓解 |
|------|------|
| 受支持格式不一致 | 服务端按 magic bytes 验真，不依赖 Content-Disposition 扩展名 |
| 上传巨型文件 DoS | 5MB 上限 + multipart parser 流式读 |
| Mock 与 real provider 行为偏离 | Result schema 统一；real provider 替换时只动 implementation |
| 真实 provider 调用慢（>10s） | 异步 + timeout；前端显示"语音识别中..."loading |

### 6. 不在本工单

- ❌ 通用 `/media/upload`（→ BE-MEDIA-001）
- ❌ 接阿里云 / 腾讯云 ASR（→ BE-MEDIA-002-real）
- ❌ TTS 合成（→ BE-MEDIA-003）

## 后续工单

- **BE-MEDIA-002-real**：阿里云一句话识别 provider
- **BE-MEDIA-001**：通用文件上传端点（avatar / 活体截图等）
- **BE-MEDIA-003**：TTS 端点 (`/audio/synthesize`)，让 Niki 会"说话"
- **BE-MEDIA-004**：audio 二级缓存（重复相同 hash 短路）
