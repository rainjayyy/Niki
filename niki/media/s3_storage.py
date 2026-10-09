"""S3-compatible object storage (BE-MEDIA-001-real).

Implements `MediaStorage` against any S3-protocol endpoint:
    - AWS S3 (default)
    - Aliyun OSS (S3-compatible mode)
    - Tencent COS (S3-compatible mode)
    - MinIO / on-prem S3

Uses `boto3` for the protocol. The boto3 import is **lazy** —
local dev (which uses `LocalDiskStorage`) doesn't need boto3
installed.

A second class, `InMemoryS3Storage`, ships for tests. It
implements the full `MediaStorage` contract against an in-process
dict, with a `presigned_url` factory that returns a
deterministic placeholder URL the API layer can serve from.
No real network calls, no `boto3` mock framework needed.
"""

from __future__ import annotations

import base64
import hashlib
import io
import re
import threading
from collections.abc import Iterator
from typing import Any

import structlog

from niki.media.storage import (
    ALLOWED_MIMES,
    MAX_FILE_BYTES,
    EmptyFileError,
    FileTooLargeError,
    MediaStorage,
    StoredContent,
    StoredFile,
    UnsupportedMimeError,
    _EXT_BY_MIME,
    _detect_mime,
)

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Real S3-compatible storage
# ---------------------------------------------------------------------------


class S3CompatibleStorage(MediaStorage):
    """Store media blobs in any S3-protocol endpoint.

    Args:
        endpoint_url:    e.g. ``https://oss-cn-hangzhou.aliyuncs.com``.
                         Empty string = use the boto3 default (AWS S3).
        region:          e.g. ``us-east-1``, ``oss-cn-hangzhou``.
        bucket:          target bucket name.
        access_key / secret_key: IAM credentials.
        addressing_style: ``"auto"`` (boto3 picks) | ``"path"`` |
                          ``"virtual"`` (Aliyun OSS recommends path).
        presign_ttl_seconds: lifetime of generated presigned URLs.
        multipart_threshold_bytes: switch to multipart above this.

    Raises:
        RuntimeError: if boto3 is not installed.
    """

    def __init__(
        self,
        *,
        endpoint_url: str = "",
        region: str = "us-east-1",
        bucket: str,
        access_key: str,
        secret_key: str,
        addressing_style: str = "auto",
        presign_ttl_seconds: int = 3600,
        multipart_threshold_bytes: int = 5 * 1024 * 1024,
    ) -> None:
        if not bucket:
            raise ValueError("bucket is required")
        try:
            import boto3
            from botocore.client import Config
        except ImportError as exc:
            raise RuntimeError(
                "S3CompatibleStorage requires boto3. Install with: pip install boto3"
            ) from exc

        self._bucket = bucket
        self._presign_ttl = presign_ttl_seconds
        self._multipart_threshold = multipart_threshold_bytes

        # boto3 `Config` — addressing_style controls URL shape
        # (virtual-host vs path). Aliyun OSS path-style:
        #   https://oss-cn-hangzhou.aliyuncs.com/<bucket>/<key>
        # AWS S3 virtual-host (default):
        #   https://<bucket>.s3.<region>.amazonaws.com/<key>
        if addressing_style == "path":
            cfg = Config(s3={"addressing_style": "path"})
        elif addressing_style == "virtual":
            cfg = Config(s3={"addressing_style": "virtual"})
        else:
            cfg = Config()  # auto

        kwargs: dict[str, Any] = {
            "region_name": region,
            "aws_access_key_id": access_key,
            "aws_secret_access_key": secret_key,
            "config": cfg,
        }
        if endpoint_url:
            kwargs["endpoint_url"] = endpoint_url

        self._client = boto3.client("s3", **kwargs)
        logger.info(
            "s3_storage.initialised",
            endpoint=endpoint_url or "default",
            bucket=bucket,
            region=region,
            addressing_style=addressing_style,
        )

    def put(
        self,
        content: bytes,
        *,
        mime_type: str | None = None,
    ) -> StoredFile:
        if not content:
            raise EmptyFileError("upload is empty")
        if len(content) > MAX_FILE_BYTES:
            raise FileTooLargeError(len(content), MAX_FILE_BYTES)

        detected = _detect_mime(content, mime_type)
        if detected is None or detected not in ALLOWED_MIMES:
            raise UnsupportedMimeError(mime_type or "unknown")
        mime = detected

        file_id = f"f-{self._hash(content)[:8]}-{self._seq()}"
        ext = _EXT_BY_MIME.get(mime, ".bin")
        key = f"{file_id}{ext}"

        if len(content) <= self._multipart_threshold:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=content,
                ContentType=mime,
                Metadata={"niki-mime": mime},
            )
        else:
            self._multipart_upload(key=key, content=content, mime=mime)

        url = self._presign(key)
        sha1 = hashlib.sha1(content).hexdigest()
        from datetime import datetime, timezone

        return StoredFile(
            file_id=file_id,
            mime_type=mime,
            size_bytes=len(content),
            sha1=sha1,
            access_url=url,
            uploaded_at=datetime.now(timezone.utc),
        )

    def _multipart_upload(self, *, key: str, content: bytes, mime: str) -> None:
        """Multipart upload for content > multipart_threshold.

        boto3's high-level `upload_fileobj` handles chunking +
        retries internally. We still need to set ContentType via
        `ExtraArgs` so HEAD returns the right MIME.
        """
        self._client.upload_fileobj(
            Fileobj=io.BytesIO(content),
            Bucket=self._bucket,
            Key=key,
            ExtraArgs={
                "ContentType": mime,
                "Metadata": {"niki-mime": mime},
            },
        )

    def get(self, file_id: str) -> bytes | None:
        key = self._key_for_id(file_id)
        if key is None:
            return None
        try:
            resp = self._client.get_object(Bucket=self._bucket, Key=key)
        except Exception as exc:  # pragma: no cover — network path
            # boto3 raises ClientError with Error.Code == "NoSuchKey"
            code = getattr(getattr(exc, "response", {}), "get", lambda *_: {})("Error", {}).get(
                "Code", ""
            )
            if code in ("NoSuchKey", "404", "NotFound"):
                return None
            logger.error("s3_storage.get_failed", key=key, error=str(exc))
            return None
        return resp["Body"].read()

    def get_with_meta(self, file_id: str) -> StoredContent | None:
        """Read body + authoritative MIME from object metadata.

        Unlike LocalDiskStorage (which guesses MIME from the file
        extension), HEADing the S3 object gives us the ContentType
        the uploader set. This is the safer path for cross-vendor
        clients (e.g. iOS sending `image/heic`).
        """
        key = self._key_for_id(file_id)
        if key is None:
            return None
        try:
            head = self._client.head_object(Bucket=self._bucket, Key=key)
        except Exception as exc:  # pragma: no cover
            code = getattr(getattr(exc, "response", {}), "get", lambda *_: {})("Error", {}).get(
                "Code", ""
            )
            if code in ("NoSuchKey", "404", "NotFound"):
                return None
            logger.error("s3_storage.head_failed", key=key, error=str(exc))
            return None
        body = self.get(file_id)
        if body is None:
            return None
        mime = head.get("ContentType") or "application/octet-stream"
        return StoredContent(
            content=body,
            mime_type=mime,
            file_id=file_id,
        )

    # ----------------- helpers -----------------

    def _presign(self, key: str) -> str:
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=self._presign_ttl,
        )

    def _key_for_id(self, file_id: str) -> str | None:
        """Look up the S3 key for a given file_id.

        We don't know whether the file was stored as `.jpg` or
        `.png` etc., so probe by listing the bucket prefix. For
        production we can store the extension in object metadata
        and read it back via HEAD — but list-based lookup keeps
        this implementation independent of metadata-write quirks.
        """
        prefix = file_id + "."
        try:
            resp = self._client.list_objects_v2(Bucket=self._bucket, Prefix=prefix, MaxKeys=5)
        except Exception as exc:  # pragma: no cover
            logger.error("s3_storage.list_failed", prefix=prefix, error=str(exc))
            return None
        for obj in resp.get("Contents", []):
            return obj["Key"]
        return None

    @staticmethod
    def _hash(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    _SEQ_LOCK = threading.Lock()
    _SEQ_COUNTER: Iterator[int] = iter(range(1, 10**9))

    @classmethod
    def _seq(cls) -> int:
        with cls._SEQ_LOCK:
            return next(cls._SEQ_COUNTER)


# ---------------------------------------------------------------------------
# In-memory fake for tests
# ---------------------------------------------------------------------------


class InMemoryS3Storage(MediaStorage):
    """In-process replacement for `S3CompatibleStorage`.

    Implements the full `MediaStorage` contract against a
    process-local dict. Presigned URLs are deterministic
    placeholders (the API layer can serve them or rewrite them
    to a real S3 URL via a one-line change).

    Use this in unit tests so we don't need a real S3 endpoint
    or a heavy `moto` mock.
    """

    def __init__(
        self,
        *,
        bucket: str = "test-bucket",
        endpoint_url: str = "https://s3.test",
        presign_ttl_seconds: int = 3600,
        public_url_base: str = "https://test-cdn.example/media",
    ) -> None:
        self._bucket = bucket
        self._endpoint = endpoint_url
        self._presign_ttl = presign_ttl_seconds
        self._public_url_base = public_url_base
        # key -> (bytes, mime, sha1)
        self._objects: dict[str, tuple[bytes, str, str]] = {}
        self._lock = threading.Lock()

    def put(
        self,
        content: bytes,
        *,
        mime_type: str | None = None,
    ) -> StoredFile:
        if not content:
            raise EmptyFileError("upload is empty")
        if len(content) > MAX_FILE_BYTES:
            raise FileTooLargeError(len(content), MAX_FILE_BYTES)
        detected = _detect_mime(content, mime_type)
        if detected is None or detected not in ALLOWED_MIMES:
            raise UnsupportedMimeError(mime_type or "unknown")
        mime = detected

        sha1 = hashlib.sha1(content).hexdigest()
        file_id = f"f-{sha1[:8]}-{self._next_seq()}"
        ext = _EXT_BY_MIME.get(mime, ".bin")
        key = f"{file_id}{ext}"

        with self._lock:
            self._objects[key] = (content, mime, sha1)

        from datetime import datetime, timezone

        return StoredFile(
            file_id=file_id,
            mime_type=mime,
            size_bytes=len(content),
            sha1=sha1,
            access_url=self._access_url(file_id, ext, mime),
            uploaded_at=datetime.now(timezone.utc),
        )

    def get(self, file_id: str) -> bytes | None:
        key = self._key_for_id(file_id)
        if key is None:
            return None
        with self._lock:
            entry = self._objects.get(key)
        return entry[0] if entry else None

    def get_with_meta(self, file_id: str) -> StoredContent | None:
        key = self._key_for_id(file_id)
        if key is None:
            return None
        with self._lock:
            entry = self._objects.get(key)
        if entry is None:
            return None
        content, mime, _sha = entry
        return StoredContent(
            content=content,
            mime_type=mime,
            file_id=file_id,
        )

    # ----------------- helpers -----------------

    _INMEM_SEQ_LOCK = threading.Lock()
    _INMEM_SEQ = 0

    @classmethod
    def _next_seq(cls) -> int:
        with cls._INMEM_SEQ_LOCK:
            cls._INMEM_SEQ += 1
            return cls._INMEM_SEQ

    def _access_url(self, file_id: str, ext: str, mime: str) -> str:
        # Synthetic URL: looks like a CDN link, parses cleanly
        # by the existing media:// route. The test endpoint
        # (/api/v1/media/{file_id:path}) accepts both.
        return f"{self._public_url_base}/{file_id}{ext}"

    def _key_for_id(self, file_id: str) -> str | None:
        prefix = file_id + "."
        for key in self._objects:
            if key.startswith(prefix):
                return key
        return None

    def __len__(self) -> int:
        return len(self._objects)
