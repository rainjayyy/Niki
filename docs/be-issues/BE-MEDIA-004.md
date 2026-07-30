# BE-MEDIA-004：头像 / 图片上传专用入口

| 字段 | 值 |
|------|-----|
| **优先级** | 🟢 P2 |
| **域** | MEDIA |
| **来源** | 前端家长端 dashboard 头像上传需求 |
| **工作量** | 0.5 天 |
| **状态** | 🚧 进行中（2026-07-30） |
| **依赖** | BE-MEDIA-001 ✅ / BE-STOR-001-ext-E ✅ |
| **后续** | 头像裁剪 / 历史头像 → BE-MEDIA-004-rollout |

## 背景

通用 `POST /api/v1/media/upload`（BE-MEDIA-001）已经能接受图片，但
对前端来说有个明显的体验问题：

- 前端上传头像 → 后端返回 `{file_id, mime_type, ...}`
- 前端拿到 `file_id` 后，**还得自己记住"这个 file_id 是头像"**
- 用户改头像时，前端再调一次 `PUT /children/{id}/avatar` 把关联写回去

对"一个孩子一张头像"这种语义明确的资源，单独做一个 **purpose-specific** 的端点
更友好：

- 端点本身就表达意图（`/media/avatar`）
- 限制更严：1 MB / 仅 jpg|png（避免前端塞进 webp / gif 导致老客户端渲染失败）
- 关联信息直接进响应（`child_id`、`kind: "avatar"`）——前端不用记 out-of-band

不引入新存储路径——头像走的就是通用 `MediaStorage.put(...)`，
未来切 OSS（BE-MEDIA-001-real）时自动跟随。

## 验收标准（AC）

- [ ] AC-1：`POST /api/v1/media/avatar` 接受 `multipart/form-data`，字段 `file` (binary)
- [ ] AC-2：返回 `{child_id, file_id, access_url, mime_type, size_bytes, sha1, category, kind}`，
        其中 `kind: "avatar"`、`category: "image"`
- [ ] AC-3：可选 form 字段 `child_id` —— 传了就要通过 lateral-auth（`PARENT_CHILD_MISMATCH` → 403）
- [ ] AC-4：文件 ≤ 1 MB；空文件 400；超限 413 (`AVATAR_TOO_LARGE`)
- [ ] AC-5：仅接受 `image/jpeg` / `image/png`（magic-byte 校验）；其他 415 (`INVALID_AVATAR_MIME`)
- [ ] AC-6：复用 `MediaStorage.put(...)`，**不引入新存储路径**
- [ ] AC-7：可与 `get_current_parent` 共存——通过 FastAPI Depends 链路
- [ ] AC-8：单元 + 集成测试 ≥ 5 个，全绿
- [ ] AC-9：现有 1453 passed 测试**零回归**

## 设计

### 1. 端点

```
POST /api/v1/media/avatar
Content-Type: multipart/form-data
Authorization: Bearer <jwt>

file=@avatar.png                              # required, ≤ 1 MB, jpg|png
child_id=c-001                                # optional

→ 200 {
    "child_id": "c-001",                      ← None if not provided
    "file_id": "f-7e9a1b3c",
    "access_url": "media://local/f-7e9a1b3c.png",
    "mime_type": "image/png",
    "size_bytes": 12345,
    "sha1": "ecc47ce86da7ff98e8a6ffe9ca2a...",
    "category": "image",
    "kind": "avatar"
  }

→ 400 EMPTY_FILE                  - file is empty
→ 403 PARENT_CHILD_MISMATCH       - token does not own this child_id
→ 404 CHILD_NOT_FOUND             - child_id unknown
→ 413 AVATAR_TOO_LARGE            - > 1 MB
→ 415 INVALID_AVATAR_MIME         - not jpg/png
→ 503 STORAGE_UNAVAILABLE         - storage backend down
```

### 2. 校验 helper

```python
def validate_image_for_avatar(
    content: bytes,
    mime_type: str | None,
    max_bytes: int = 1_048_576,
) -> str:
    """Returns the magic-detected mime if jpg/png and size ≤ max.

    Raises:
        HTTPException(413) — too large
        HTTPException(415) — wrong mime
    """
```

- 大小先校验（便宜），再 magic-byte 探测 MIME
- 仅 `image/jpeg` / `image/png` 通过——比通用上传（jpg/png/webp/gif）更严

### 3. Lateral-auth

复用 `niki.auth.dependencies.get_owned_child_id` 的**逻辑**，
但因为 `child_id` 来自 form 而非路径，
所以在 media.py 里写了一个**同样带审计**的版本
（`_assert_parent_owns_child`）：

- `CHILD_NOT_FOUND` → 404（带 audit event）
- `PARENT_CHILD_MISMATCH` → 403（带 audit event）
- 测试模式 `NIKI_TEST_FAKE_AUTH=1` 短路

将来如要复用，可抽出到 `auth/dependencies.py`。

### 4. 路由顺序

FastAPI 按声明顺序匹配路径。
`POST /avatar` 注册在 `GET /{file_id:path}` **之前**，
所以不会被 `{file_id:path}` 截胡。

### 5. 风险

| 风险 | 缓解 |
|------|------|
| 上传巨大文件 DoS | 1 MB 上限（在读完整 bytes 之前检查长度） |
| 错 MIME（前端说 png 但实际 webp） | magic-byte 校验（`image/webp` 在 jpg/png 之外） |
| 跨家长冒名传头像 | lateral-auth + audit event |
| 文件 `file_id` 漂移 | 复用通用 upload 的 `MediaStorage`，未来切 OSS 自动跟随 |

### 6. 不在本工单

- ❌ 头像裁剪 / 自动 resize（→ BE-MEDIA-004-rollout）
- ❌ 头像历史版本管理（→ BE-MEDIA-004-rollout）
- ❌ PUT /children/{id}/avatar 同步绑定（v1 阶段前端手动保存 file_id）
- ❌ WebP / GIF 支持（前端如有需求单独开 AC）

## 后续工单

- **BE-MEDIA-004-rollout**：头像裁剪（服务端自动 256x256 / 512x512）+ 历史版本