"""Min client version middleware (BE-COMP-003).

Reads `X-Client-Version` from the request and rejects with
426 Upgrade Required if the version is older than
`settings.min_supported_version`. Missing header is
allowed (back-compat: older clients may not send it).
"""

from __future__ import annotations

import re
import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = structlog.get_logger(__name__)


_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def _parse_semver(s: str) -> tuple[int, int, int] | None:
    """Parse a 3-part semver like '1.2.3' into a comparable
    tuple. Returns None on garbage.
    """
    m = _SEMVER_RE.match(s.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


class MinClientVersionMiddleware(BaseHTTPMiddleware):
    """Reject clients below `min_supported_version` with 426.

    Two design choices:
      - Missing header is allowed (back-compat with very old
        clients that don't send the header at all).
      - Headers that fail to parse are also allowed (we
        prefer false-negatives over false-positives).
    """

    HEADER = "X-Client-Version"

    def __init__(self, app, *, min_supported_version: str) -> None:
        super().__init__(app)
        self._min = _parse_semver(min_supported_version)
        if self._min is None:
            raise ValueError(
                f"invalid min_supported_version: "
                f"{min_supported_version!r}"
            )

    async def dispatch(
        self, request: Request, call_next
    ) -> Response:
        ver = request.headers.get(self.HEADER)
        if ver:
            parsed = _parse_semver(ver)
            if parsed is not None and parsed < self._min:
                logger.info(
                    "client_version.too_old",
                    client_version=ver,
                    min_version=".".join(str(x) for x in self._min),
                )
                return JSONResponse(
                    status_code=426,
                    content={
                        "error": "UPGRADE_REQUIRED",
                        "message": (
                            f"client version {ver} is below the "
                            f"minimum supported version"
                        ),
                        "min_supported_version": ".".join(
                            str(x) for x in self._min
                        ),
                    },
                    headers={
                        "Upgrade": ".".join(
                            str(x) for x in self._min
                        ),
                        "Retry-After": "86400",
                    },
                )
        return await call_next(request)
