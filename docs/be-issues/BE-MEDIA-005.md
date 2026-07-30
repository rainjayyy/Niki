# BE-MEDIA-005：OCR 端点 + Mock 后端

| 字段 | 值 |
|------|-----|
| **优先级** | 🟢 P2 |
| **域** | MEDIA |
| **来源** | 百科（encyclopedia）agent 接收 `image_url` 后需要调 OCR，但目前没有图片上传+OCR 接口 |
| **工作量** | 0.5 天 |
| **状态** | 🚧 进行中（2026-07-30） |
| **依赖** | BE-MEDIA-001 ✅ / BE-MEDIA-004 ✅ |
| **后续** | BE-MEDIA-005-real：接入真实 OCR（pytesseract / 阿里云 / 腾讯） |

## 背景

百科 agent 现在的 prompt 模板里有个分支：当用户的问题是“图片里有什么？”时，
后端需要先把这张图丢给 OCR 服务拿到文字，然后再把这些文字塞回 prompt：

```
用户上传一张图片 → 后端下载 → 调用 OCR 服务 → 把识别出的文字 + 用户原始问题
→ 一起塞进 LLM prompt
```

但服务端目前**没有挂 OCR**：

- 通用 `POST /api/v1/media/upload` 只能存字节，不会触发识别
- 也没有任何 `/ocr` 端点可以“上传图片 → 拿到识别结果”

本工单先搭出一个最小可用接口：
- 新增 `OcrBackend` 抽象（与 `ASRProvider` / `TTSProvider` 同形态）
- 实现 `MockOcrBackend`：基于图片 SHA-1 哈希从一个小词典里确定性挑词，
  没有任何外部依赖，CI / 单元测试可以直接跑
- 暴露 `POST /api/v1/media/ocr`：multipart 上传 → `{text, language, confidence, regions}`

接真实 OCR 服务留给 `BE-MEDIA-005-real`，**接口已经定死**，后续只换 backend。

## 验收标准（AC）

- [ ] AC-1：`niki/media/ocr.py` 新增 `OcrResult` / `OcrRegion` dataclass
- [ ] AC-2：`OcrBackend` ABC 含 `async recognize(image, language)` 方法
- [ ] AC-3：`MockOcrBackend` 返回**确定性**结果（同一图片永远同一段文字）
- [ ] AC-4：进程级单例 `get_ocr_backend()` / `set_ocr_backend()`
- [ ] AC-5：`POST /api/v1/media/ocr` 接受 `multipart/form-data`，字段 `file`
- [ ] AC-6：返回 `{text, language, confidence, regions}`
- [ ] AC-7：仅接受图片（jpg / png / webp / gif，magic-byte 校验）；其他 415
- [ ] AC-8：≤ 5 MB；空文件 400；超限 413 (`OCR_IMAGE_TOO_LARGE`)
- [ ] AC-9：可选 form 字段 `language`（BCP-47）；默认 `"zh-CN"`
- [ ] AC-10：backend 失败映射到 503（如 `OcrError("OCR_QUOTA_EXCEEDED", ...)`）
- [ ] AC-11：测试覆盖 happy path / 413 / 415 / 400 / 后端失败 / 确定性
- [ ] AC-12：现有 1553 passed 测试**零回归**（+2 skipped 不变）

## 设计

### 1. 后端抽象

```python
@dataclass(frozen=True)
class OcrRegion:
    text: str
    confidence: float
    bbox: tuple[int, int, int, int] = (0, 0, 0, 0)

@dataclass(frozen=True)
class OcrResult:
    text: str
    language: str
    confidence: float
    regions: tuple[OcrRegion, ...] = ()

class OcrBackend(ABC):
    @abstractmethod
    async def recognize(self, image, *, language="zh-CN") -> OcrResult: ...
```

### 2. Mock 后端

`MockOcrBackend.recognize` 不调任何 OCR 服务，纯粹基于
`hashlib.sha1(image).digest()` 的字节：

- 用 `digest[4..5]` 决定挑几个词（**3 ~ 5**）
- 用连续字节做模 N 桶选，**确定性**：同一图片永远同一段文字
- 词典按 language 分：
  - `zh*` → 20 个中文名词（`苹果`、`太阳`、...）
  - `en*` → 20 个英文名词（`apple`、`sun`、...）
- region confidence 在 `0.88 ~ 0.96` 之间小幅抖动，模拟真实

伪代码：

```python
digest = hashlib.sha1(image).digest()
n = 3 + ((digest[4] << 8 | digest[5]) % 3)         # 3..5
words = _MOCK_WORDS_EN if lang.startswith("en") else _MOCK_WORDS_ZH
picked = [bucket[i] for i in bucket_indices from digest]   # 长度 n
text = " ".join(picked)
```

没有外部依赖（不引 `pytesseract`），CI 能跑。

### 3. 端点

```
POST /api/v1/media/ocr
Content-Type: multipart/form-data
Authorization: Bearer <jwt>

file=@shot.jpg                                       # required, ≤ 5 MB, image/*
language=zh-CN                                       # optional, BCP-47

→ 200 {
    "text": "苹果 太阳 学校 妈妈",                  # space-joined recognised words
    "language": "zh-CN",
    "confidence": 0.913,                              # mean of per-region confs
    "regions": [
      {"text": "苹果", "confidence": 0.91, "bbox": [0, 0, 10, 16]},
      {"text": "太阳", "confidence": 0.88, "bbox": [10, 0, 20, 16]},
      ...
    ]
  }

→ 400 EMPTY_FILE              - file is empty
→ 413 OCR_IMAGE_TOO_LARGE     - > 5 MB
→ 415 UNSUPPORTED_OCR_MIME    - 不是 image/jpeg|png|webp|gif
→ 503 OCR_<CODE>              - backend 抛 OcrError
```

#### 校验顺序（与通用 upload 一致）

1. 读出 `raw = await file.read()`
2. **空文件** → 400 EMPTY_FILE
3. **大小** → 超过 5 MB → 413 OCR_IMAGE_TOO_LARGE
4. **MIME**：`_detect_mime(raw, claimed)` 必须在 4 种图片白名单里，
   否则 415 UNSUPPORTED_OCR_MIME
5. **调 backend**：捕获 `OcrError` → 503

和 `POST /media/upload`（10 MB / image+audio）相比：

- 上限更紧：**5 MB**（OCR 输入图片通常更小）
- MIME 白名单更窄：**只图、不要音**（音频 OCR 没意义）

### 4. 路由顺序

`POST /media/ocr` 注册在 `POST /media/avatar` 之前，
在 `GET /media/{file_id:path}` 之前——不会被 `{file_id:path}` 截胡。

### 5. 风险

| 风险 | 缓解 |
|------|------|
| 用真 OCR 服务导致 PoC 不可用 | v1 mock 兜底，后续 ticket 切真 |
| 大文件 DoS | 5 MB 上限（在 backend 调用之前） |
| 错 MIME（前端说 png 实际是文本） | magic-byte 校验 |
| Mock 文字不可信 | 文档明确标注“demo only”，`BE-MEDIA-005-real` 才接真服务 |
| Singleton 状态泄露（test 改了 backend 影响后续用例） | conftest 的 `_reset_*` 系列已预留；测试自己在 fixture 里恢复 |

### 6. 不在本工单

- ❌ 真 OCR 接入（→ BE-MEDIA-005-real）
- ❌ 与百科 agent 的端到端串联（→ BE-RT-002-rollout）
- ❌ 异步 / 流式识别（v1 同步即可）
- ❌ 多语言模型选择（v1 只一个 mock）

## 后续工单

- **BE-MEDIA-005-real**：接入 `pytesseract` 或云 OCR（Aliyun / Tencent）
  - 新增 `TesseractOcrBackend` / `AliyunOcrBackend` 等具体实现
  - `settings.ocr_backend` 驱动选择
  - 配额 / 超时 / 费用埋点
- **BE-RT-002-rollout**：百科 agent 通过 `media_url` 自动调 OCR
- **BE-MEDIA-005-i18n**：OCR 错误码国际化（与 `media.*` 词典对齐）
