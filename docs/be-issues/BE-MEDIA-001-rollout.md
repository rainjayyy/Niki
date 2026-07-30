# BE-MEDIA-001-rollout：CDN 缓存 + 图片处理

| 字段 | 值 |
|------|-----|
| **优先级** | 🟡 P1 |
| **域** | MEDIA / PERFORMANCE |
| **来源** | 前端对接文档 §7.2.6（OSS 后续优化） |
| **工作量** | 0.5 天 |
| **状态** | 🚧 进行中（2026-07-29） |
| **依赖** | BE-MEDIA-001-real ✅（OSS provider 已上） |
| **后续** | WebP 转换 / 缩略图 → BE-MEDIA-001-rollout-rollout |

## 背景

BE-MEDIA-001-real 给了生产**对象存储后端**（S3 / 阿里云 OSS），但**下载体验**还没优化：

- 家长端 / 儿童端每次拉图片都从 OSS 重新取 → OSS 流量费 = `月活用户数 × 图片数 × 平均大小`
- 没有 `Cache-Control` header，浏览器无法缓存 → 用户反复看同一张图，每次都打 OSS
- 没有图片处理（缩略图 / WebP / 旋转）→ 用户下 5MB 的原图只为了显示 200×200 缩略图

本工单做 2 件事：
1. **CDN 缓存 headers** —— `Cache-Control: public, max-age=86400` 让浏览器/CDN 缓存 24h
2. **服务端图片处理** —— 缩略图端点 `?w=200` 按需返回缩小版

## 验收标准（AC）

- [ ] AC-1：`GET /api/v1/media/{file_id}` 响应加 `Cache-Control: public, max-age=86400`
- [ ] AC-2：缩略图端点 `GET /api/v1/media/{file_id}/thumb?w=200&h=200` 走 OSS GetObject
- [ ] AC-3：缩略图尺寸限制 `1 ≤ w ≤ 2048` / `1 ≤ h ≤ 2048`（防滥用）
- [ ] AC-4：缩略图用 `BytesIO` 缓存 100 张（LRU，in-process）
- [ ] AC-5：原图缓存**不**经过缩略图端点（保持原样）
- [ ] AC-6：测试覆盖 ≥ 6 个，含：cache header / thumb 生成 / 尺寸校验 / LRU eviction
- [ ] AC-7：现有 1318 passed 测试**零回归**

## 设计

### 1. CDN Headers

`/api/v1/media/{file_id}` 响应添加：
```
Cache-Control: public, max-age=86400
ETag: "<sha1>"
Vary: Accept-Encoding
```

ETag 用 sha1（已在 StoredFile 里），让浏览器走 304 Not Modified。

### 2. 缩略图端点

```
GET /api/v1/media/{file_id}/thumb?w=200&h=200
→ 200 image/jpeg (or original mime)
   Cache-Control: public, max-age=86400
```

实现：用 Pillow (PIL) 的 `thumbnail()` 方法。
- 读取原图（走 OSS GetObject）
- 缩放到 (w, h) 边界内
- 编码回 bytes
- 缓存到 in-memory LRU

### 3. LRU 缓存

```python
from functools import lru_cache

# 用 functools.lru_cache 不行（参数不可哈希 → bytes），
# 自实现：
class LRU(Generic[T]):
    def __init__(self, maxsize: int = 100): ...
    def get(self, key: str) -> T | None: ...
    def set(self, key: str, value: T) -> None: ...
```

Key = `{file_id}:{w}:{h}`；Value = bytes（缩略图）。

### 4. 风险

| 风险 | 缓解 |
|------|------|
| 缩略图内存爆 | LRU 100 张 + 限 max w/h 2048 |
| 恶意请求耗资源 | rate limit 复用 `BE-API-002`（同 endpoint 池） |
| 缓存陈旧 | ETag / Cache-Control 控制 |

### 5. 不在本工单

- ❌ WebP 转换（→ 后续）
- ❌ CDN 真实接入（依赖运维提供 CDN 域名）
- ❌ 图片 EXIF 旋转（→ 后续）