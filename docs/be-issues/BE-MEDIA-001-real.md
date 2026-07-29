# BE-MEDIA-001-real：对象存储（OSS / COS）支持

| 字段 | 值 |
|------|-----|
| **优先级** | 🟡 P1 |
| **域** | MEDIA / INFRA |
| **来源** | 前端对接文档 §7.2.6（BE-STOR-003） |
| **工作量** | 1-2 天 |
| **状态** | 🚧 进行中（2026-07-29） |
| **依赖** | `MediaStorage` ABC（BE-MEDIA-001 ✅） |
| **后续** | CDN 缓存 / 图片处理（缩略图 / 旋转）→ BE-MEDIA-001-rollout |

## 背景

当前 `LocalDiskStorage` 把上传文件落到服务器本地磁盘：

```python
# niki/media/storage.py
class LocalDiskStorage(MediaStorage):
    def put(self, content, *, mime_type=None):
        ...
        path.write_bytes(content)
        return StoredFile(
            access_url=f"media://local/{file_id}{ext}",
            ...
        )
```

后果：
- 多 worker / 多实例部署 → 本地文件不同步
- 容器外不持久（K8s pod 重启 = 文件丢）
- 没有 CDN，下载延迟看服务器带宽
- 没有签名直传，前端必须走后端中转

**Aliyun OSS / 腾讯 COS / AWS S3 都是 S3 协议兼容**。一个抽象层 + S3 客户端就能覆盖三种生产环境。

本工单补齐 S3 兼容存储后端。`boto3` 是 AWS 官方 SDK，对 S3 协议 100% 兼容。Aliyun OSS / 腾讯 COS 都有 S3 兼容模式可直接复用。

## 验收标准（AC）

- [ ] AC-1：新增 `S3CompatibleStorage` 实现 `MediaStorage` ABC
- [ ] AC-2：`put` 走 S3 `PutObject` API，`<= 5MB` 单次 PUT，`> 5MB` 自动 multipart upload
- [ ] AC-3：`get` 走 S3 `GetObject` API，返回 bytes
- [ ] AC-4：`get_with_meta` 走 S3 `HeadObject` API，从 object metadata 读 authoritative `mime_type`
- [ ] AC-5：`access_url` 返回 presigned HTTPS URL（默认 1 小时有效）
- [ ] AC-6：环境变量缺失 / `boto3` 未装 → 显式报错（不静默降级）
- [ ] AC-7：测试用 `InMemoryS3Storage`（boto3 mock via `moto` 或自带 fake），无需真实 OSS
- [ ] AC-8：测试覆盖 ≥ 6 个，含：put/get roundtrip / mime from metadata / access_url 格式 / mock 模式
- [ ] AC-9：现有 1237 passed 测试**零回归**

## 设计

### 1. Settings

```python
class Settings(BaseSettings):
    # BE-MEDIA-001-real: object storage
    media_storage_backend: str = "local"          # "local" | "s3"
    s3_endpoint_url: str = ""                    # 空 = AWS S3
    s3_region: str = "us-east-1"
    s3_bucket_name: str = "niki-media"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_presign_ttl_seconds: int = 3600           # 1h
    s3_multipart_threshold_bytes: int = 5 * 1024 * 1024  # 5MB
    s3_multipart_chunk_size_bytes: int = 5 * 1024 * 1024
```

### 2. Provider 切换

```python
def get_media_storage() -> MediaStorage:
    settings = get_settings()
    if settings.media_storage_backend == "s3":
        return S3CompatibleStorage(...)
    return LocalDiskStorage(...)
```

（与现有 `get_media_storage()` 模式一致）

### 3. S3CompatibleStorage

```python
class S3CompatibleStorage(MediaStorage):
    def __init__(self, *, endpoint_url, region, bucket, access_key, secret_key,
                 presign_ttl=3600, multipart_threshold=5*1024*1024, ...):
        import boto3  # lazy import — local dev 不强制装 boto3
        self._client = boto3.client("s3", ...)
        ...
```

put: 单次或 multipart，按 `multipart_threshold` 自动切
get: `client.get_object(Bucket, Key)` → body.read()
get_with_meta: `client.head_object(Bucket, Key)` → ContentType from metadata
presigned URL: `client.generate_presigned_url('get_object', ...)` → HTTPS URL

### 4. 测试 FakeS3

避免依赖真实 OSS 账号。用 `moto` 库（`pip install moto[s3]`）in-process mock。

或者实现 `InMemoryS3Storage`：纯 dict 实现，验证 ABC 契约（put / get / presign URL 格式）。

### 5. 风险

| 风险 | 缓解 |
|------|------|
| boto3 升级破坏 API | boto3 是 AWS 官方 SDK，3.x 一直稳定 |
| Aliyun OSS 签名 V4 vs V1 | boto3 默认 V4 + path-style 走通用 S3，OSS 兼容模式可开 `AddressingStyle: virtual` |
| multipart 上传失败回滚 | 用 `S3.Client.complete_multipart_upload` 失败时 abort 清理 |
| Presigned URL 泄漏 | 短期 TTL（默认 1h），按租户可调 |

### 6. 不在本工单

- ❌ CDN 接入（→ BE-MEDIA-001-rollout）
- ❌ 图片处理（缩略图 / 旋转）→ 后续
- ❌ 真 OSS 账号 E2E 测试（依赖运维提供测试 bucket）

## 后续工单

- **BE-MEDIA-001-rollout**：CDN 缓存 / 签名 URL 长期化 / 图片处理
- **BE-STOR-003**：OSS 备份（数据导出 / treehole 加密 blob）