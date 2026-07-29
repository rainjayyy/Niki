# BE-MEDIA-001-rollout：文件下载端点

| 字段 | 值 |
|------|-----|
| **优先级** | 🟡 P1 |
| **域** | MEDIA |
| **来源** | BE-MEDIA-001 后续工单（README §后续） |
| **工作量** | 半天 |
| **状态** | 🚧 进行中（2026-07-29） |
| **依赖** | BE-MEDIA-001 ✅ |
| **后续** | BE-MEDIA-001-real（OSS / COS 适配） |

## 背景

BE-MEDIA-001 实现了上传 `POST /api/v1/media/upload`，返回 `access_url = "media://local/<file_id>.<ext>"`。这个 URL 是**合成 scheme**，demo 前端能渲染但浏览器 / 实际场景拿不到内容。**真实下载链路没有补全**：

- 前端拿到 `media://local/f-abc.png` 想 `<img src=...>` → 浏览器不认识 scheme → ❌
- 百科图片 / 头像 / 活体照片需要可被 HTTP 检索
- PRD 文档 §7.2.4 要求前端"接 `image_url` 直接渲染"

本工单补齐 GET 端点，让上传后的 `access_url` 能在浏览器 / 跨域场景直接被拉取。

## 验收标准（AC）

- [ ] AC-1：`GET /api/v1/media/{file_id}` 接受两种入参：
    - 裸 file_id：`f-abc12345`
    - 完整 access_url：`media://local/f-abc12345.png`
- [ ] AC-2：返回 `200` + 二进制 + 正确 `Content-Type`（按上传时的 mime）
- [ ] AC-3：`Content-Length` 与 `Cache-Control: private, max-age=86400` 头存在
- [ ] AC-4：不存在的 file_id → `404 {error: FILE_NOT_FOUND}`
- [ ] AC-5：path-traversal 类输入（`../../etc/passwd`）→ `404`（不冒 500）
- [ ] AC-6：未鉴权 → `401`（与 upload 端点一致的 Bearer 要求）
- [ ] AC-7：`MediaStorage` ABC 新增 `get_with_meta() -> StoredContent | None` 方法，`LocalDiskStorage` 默认实现
- [ ] AC-8：单元 + 集成测试 ≥ 6 个，全绿
- [ ] AC-9：现有 1097 passed 测试**零回归**

## 设计

### 1. 端点

```
GET /api/v1/media/f-7e9a1b3c
Authorization: Bearer <token>

→ 200 <binary>
   Content-Type: image/jpeg
   Content-Length: 12345
   Cache-Control: private, max-age=86400

GET /api/v1/media/media://local/f-7e9a1b3c.png
→ 200 同上（自动剥离 scheme + 扩展名）

GET /api/v1/media/f-deadbeef
→ 404 { "error": "FILE_NOT_FOUND" }

GET /api/v1/media/../../etc/passwd
→ 404 { "error": "FILE_NOT_FOUND" }   # 防 path traversal

(无 token)
→ 401 { "error": "MISSING_TOKEN" }
```

### 2. 存储契约扩展

新增 `StoredContent` dataclass + ABC 方法：

```python
@dataclass(frozen=True)
class StoredContent:
    content: bytes
    mime_type: str

class MediaStorage(ABC):
    @abstractmethod
    def put(...) -> StoredFile: ...
    @abstractmethod
    def get(self, file_id: str) -> bytes | None: ...
    # 默认实现按扩展名反查 mime；OSS 实现可重写以查询对象元数据
    def get_with_meta(self, file_id: str) -> StoredContent | None: ...
```

`LocalDiskStorage.get_with_meta()` 按 `f-xxx.<ext>` glob → 读 bytes → 按扩展名反查 `_MIME_BY_EXT`。

### 3. 安全

- **鉴权**：与 upload 一致，依赖 `get_current_parent`；未登录 `401`
- **path traversal**：`LocalDiskStorage.get_with_meta()` 用 `re.fullmatch(r"f-[0-9a-f]{1,32}", file_id)` 严格校验
- **大小**：上限继承 10 MB（已在 put 时校验，get 不重新查）
- **CDN 未来**：本期用 `private, max-age=86400`，生产替换为签名 URL（→ BE-MEDIA-001-real）

### 4. 不在本工单

- ❌ OSS / COS provider（→ BE-MEDIA-001-real）
- ❌ 缩略图（→ BE-MEDIA-001-ext-A）
- ❌ HEAD 请求（→ 后续需要时再补）
- ❌ 公开访问（无 token）——所有读都需 auth

## 后续工单

- **BE-MEDIA-001-real**：OSSStorage 实现 `get_with_meta` 通过对象元数据
- **BE-MEDIA-001-ext-A**：服务端缩略图（GET /{id}/thumb.jpg）