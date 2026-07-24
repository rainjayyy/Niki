# BE-MEDIA-001：通用文件上传端点

| 字段 | 值 |
|------|-----|
| **优先级** | 🟡 P1 |
| **域** | MEDIA |
| **来源** | `docs/frontend-integration.md` §7.2.4 |
| **工作量** | 1-2 天 |
| **状态** | 🚧 进行中（2026-07-24） |
| **依赖** | BE-MEDIA-002 ✅ / BE-MEDIA-003 ✅ |
| **后续** | BE-MEDIA-001-real 接 OSS / COS |

## 背景

§7.2.4 明确指出：

> BE-MEDIA-001 | 文件上传接口 | 百科 Agent 接 `image_url` 但**无上传端点** | 儿童端拍照识别功能跑不起来

实际痛点：
- 儿童端想拍个动物照片 → Niki 识别 → 没上传接口，**儿童端拍照识别功能完全跑不起来**
- 家长端想换头像 → 不支持上传
- 实时活体照片（face 步骤要求 `live_image_url`）→ 后端只能接受 external URL，无法直接上传

本工单为后端补上接受上传的"最后一公里"。

## 验收标准（AC）

- [ ] AC-1：`POST /api/v1/media/upload` 接受 `multipart/form-data`，字段 `file` (binary)
- [ ] AC-2：返回 `{file_id, mime_type, size_bytes, sha1, access_url, uploaded_at}`
- [ ] AC-3：文件大小 ≤ 10 MB；空文件 400；超限 413
- [ ] AC-4：拒绝不支持的 MIME 类型（仅 `image/jpeg|png|webp|gif` 和 `audio/wav|m4a|opus|mp3`）；其他 415
- [ ] AC-5：`MediaStorage` ABC + `LocalDiskStorage` 默认实现
- [ ] AC-6：相同内容上传两次 → 同一 `sha1`（deduplication-friendly）
- [ ] AC-7：单元 + 集成测试 ≥ 8 个，全绿
- [ ] AC-8：现有 952 passed 测试**零回归**

## 设计

### 1. 端点

```
POST /api/v1/media/upload
Content-Type: multipart/form-data

file=@avatar.jpg                              # required

→ 200 {
    "file_id": "f-7e9a1b3c",                  ← random uuid
    "mime_type": "image/jpeg",
    "size_bytes": 12345,
    "sha1": "ecc47ce86da7ff98e8a6ffe9ca2a...",
    "access_url": "media://local/f-7e9a1b3c", ← synthetic for dev
    "uploaded_at": "2026-07-24T16:30:00Z",
    "category": "image"
  }

→ 400 { "error": "EMPTY_FILE" | "INVALID_MIME_TYPE" }
→ 413 { "error": "FILE_TOO_LARGE", "max_bytes": 10485760 }
```

### 2. 存储抽象

```python
class MediaStorage(ABC):
    @abstractmethod
    def put(self, content: bytes, mime: str) -> StoredFile:
        """Persist bytes; returns {file_id, sha1, access_url, ...}."""
    @abstractmethod
    def get(self, file_id: str) -> bytes | None:
        """Read back the file (for retrieval endpoints later)."""

class LocalDiskStorage(MediaStorage):
    """Saves to settings.temp_upload_dir; URLs are media://local/{id}."""
```

Production swap (BE-MEDIA-001-real): implement `OSSStorage` returning `https://oss-cn-hangzhou.aliyuncs.com/...` URLs.

### 3. 触发流程

```
[儿童端拍照] → MediaRecorder / Camera API capture
                ↓ Blob → FormData
                ↓ POST /media/upload multipart
[后端]
   1. validate size <= 10MB
   2. detect MIME via magic bytes (not just Content-Type)
   3. compute sha1
   4. storage.put(bytes, mime)  → {file_id, access_url, ...}
   5. return metadata
                ↓
[前端]
   - 头像：直接 <img src={access_url}>
   - 百科拍照：把 access_url 喂给 POST /encyclopedia/ask {image_url}
   - 活体：把 access_url 喂给 POST /auth/signup/face {live_image_url}
```

### 4. 风险

| 风险 | 缓解 |
|------|------|
| 上传巨型文件 DoS | 10MB 上限；FastAPI 流式读，不一次进内存 |
| 错误 MIME 上传 (exe 当 image) | magic-byte 校验（不是 Content-Type） |
| 恶意 HTML / SVG XSS | 拒绝 `text/html`/`image/svg+xml` |
| 多实例下文件路径 | LocalDiskStorage 单机；OSS 后续 |
| Dedup key 冲突 | sha1 重复也允许 → 同 file_id 不强制唯一 |

### 5. 不在本工单

- ❌ OSS / COS 适配（→ BE-MEDIA-001-real）
- ❌ 缩略图生成（→ BE-MEDIA-001-ext-A）
- ❌ GET /media/{file_id}/raw 端点（→ 后续，前端 demo 可暂时占位）

## 后续工单

- **BE-MEDIA-001-real**：阿里云 OSS / 腾讯云 COS provider
- **BE-MEDIA-001-ext-A**：服务端图片缩略图
- **BE-MEDIA-001-GET**：`GET /api/v1/media/{file_id}/raw` 公开读取
