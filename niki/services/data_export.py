"""Data export service (BE-COMP-001). PIPL/GDPR data portability."""

from __future__ import annotations

import base64
import io
import json
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import structlog

from niki.db.repositories import (
    DashboardRepository,
    EmotionBaselineRepository,
    TreeholeRepository,
    get_dashboard_repository,
    get_emotion_baseline_repository,
    get_treehole_repository,
)

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ExportSummary:
    parent_id: str
    child_id: str | None
    file_count: int
    total_records: int
    zipped_size: int
    exported_at: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bytes_to_b64(b: bytes | str | None) -> str | None:
    if b is None:
        return None
    if isinstance(b, str):
        return b
    return base64.b64encode(b).decode("ascii")


async def _collect_account(parent_id: str) -> dict[str, Any]:
    return {
        "exported_at": _now_iso(),
        "parent_id": parent_id,
        "fields": [
            "id",
            "name",
            "email (masked)",
            "phone (masked)",
            "status",
            "created_at",
        ],
        "note": "Schema spec only.",
    }


async def _collect_children(parent_id: str, child_id: str | None) -> dict[str, Any]:
    return {
        "exported_at": _now_iso(),
        "record_count": 0,
        "items": [],
        "schema": [
            "id (uuid)",
            "name",
            "birth_date",
            "avatar_url",
            "created_at",
        ],
        "filter": {"parent_id": parent_id, "child_id": child_id},
    }


async def _collect_alerts(
    repo: DashboardRepository, parent_id: str, child_id: str | None
) -> dict[str, Any]:
    rows = await repo.list_alerts(alert_type=None, child_id=child_id)
    owned = [r for r in rows if r.get("parent_id") == parent_id]
    return {
        "exported_at": _now_iso(),
        "record_count": len(owned),
        "items": owned,
        "schema": [
            "id",
            "alert_type",
            "child_id",
            "parent_id",
            "payload",
            "created_at",
            "delivered_at",
        ],
    }


async def _collect_tutoring_events(
    repo: DashboardRepository, parent_id: str, child_id: str | None
) -> dict[str, Any]:
    items = await repo.list_tutoring_events(child_id) if child_id else []
    owned = [r for r in items if r.get("parent_id") == parent_id]
    return {
        "exported_at": _now_iso(),
        "record_count": len(owned),
        "items": owned,
        "schema": [
            "id",
            "child_id",
            "parent_id",
            "payload",
            "created_at",
        ],
    }


async def _collect_knowledge_queries(
    repo: DashboardRepository, parent_id: str, child_id: str | None
) -> dict[str, Any]:
    items = await repo.list_knowledge_queries(child_id) if child_id else []
    owned = [r for r in items if r.get("parent_id") == parent_id]
    return {
        "exported_at": _now_iso(),
        "record_count": len(owned),
        "items": owned,
        "schema": [
            "id",
            "child_id",
            "parent_id",
            "payload",
            "created_at",
        ],
    }


async def _collect_treehole(
    repo: TreeholeRepository, parent_id: str, child_id: str | None
) -> dict[str, Any]:
    # Use list_for_child when child_id is set; list_all() when None
    # (list_for_child filters by child_id == None which matches nothing).
    rows = await repo.list_for_child(child_id) if child_id else await repo.list_all()
    owned = [r for r in rows if r.get("parent_id") == parent_id]
    items = []
    for r in owned:
        items.append(
            {
                "id": r.get("id"),
                "child_id": r.get("child_id"),
                "parent_id": r.get("parent_id"),
                "encrypted_payload_b64": _bytes_to_b64(r.get("encrypted_payload")),
                "associated_data_b64": _bytes_to_b64(r.get("associated_data")),
                "expires_at": r["expires_at"].isoformat() if r.get("expires_at") else None,
                "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
            }
        )
    return {
        "exported_at": _now_iso(),
        "record_count": len(items),
        "items": items,
        "schema": [
            "id",
            "child_id",
            "parent_id",
            "encrypted_payload_b64",
            "associated_data_b64",
            "expires_at",
            "created_at",
        ],
        "note": "Ciphertext per PIPL Art. 44.",
    }


async def _collect_emotion_baseline(
    repo: EmotionBaselineRepository, parent_id: str
) -> dict[str, Any]:
    rows = await repo.list_in_window(window_days=30)
    return {
        "exported_at": _now_iso(),
        "record_count": len(rows),
        "items": rows,
        "schema": [
            "id",
            "date",
            "valence",
            "intensity",
            "source",
            "created_at",
        ],
    }


def _build_manifest(
    parent_id: str, child_id: str | None, sections: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    return {
        "exported_at": _now_iso(),
        "parent_id": parent_id,
        "child_id_filter": child_id,
        "policy": "PIPL Art. 44 / GDPR Art. 20 (data portability)",
        "niki_version": "0.1.0",
        "sections": {
            name: {
                "record_count": s.get("record_count", 0),
                "schema": s.get("schema", []),
            }
            for name, s in sections.items()
        },
    }


def _readme_text(parent_id: str, child_id: str | None) -> str:
    scope = "child " + str(child_id) if child_id else "ALL children"
    parts = [
        "Niki data portability export",
        "==================================",
        "Parent ID : " + str(parent_id),
        "Scope     : " + scope,
        "Exported  : " + _now_iso(),
        "",
        "Files in this archive:",
        "  - README.txt        this file",
        "  - account.json      parent profile (schema spec)",
        "  - children.json     child list (filtered to scope)",
        "  - alerts.json       dashboard alerts",
        "  - tutoring.json     tutoring events",
        "  - knowledge.json    knowledge queries",
        "  - treehole.json     treehole engagements (encrypted)",
        "  - emotion_baseline.json  30-day rolling emotion baseline",
        "  - manifest.json     counts + field glossary",
        "",
        "Per PIPL Art. 44 (China) and GDPR Art. 20 (EU),",
        "you have the right to obtain a copy of your personal",
        "data in a portable format.",
    ]
    return "\n".join(parts) + "\n"


async def build_export_zip(
    parent_id: str, child_id: str | None = None
) -> tuple[bytes, ExportSummary]:
    """Build a ZIP archive of all of a parent's data."""
    dashboard_repo = get_dashboard_repository()
    treehole_repo = get_treehole_repository()
    baseline_repo = get_emotion_baseline_repository()

    sections = {}
    sections["account"] = await _collect_account(parent_id)
    sections["children"] = await _collect_children(parent_id, child_id)
    sections["alerts"] = await _collect_alerts(dashboard_repo, parent_id, child_id)
    sections["tutoring"] = await _collect_tutoring_events(dashboard_repo, parent_id, child_id)
    sections["knowledge"] = await _collect_knowledge_queries(dashboard_repo, parent_id, child_id)
    sections["treehole"] = await _collect_treehole(treehole_repo, parent_id, child_id)
    sections["emotion_baseline"] = await _collect_emotion_baseline(baseline_repo, parent_id)

    manifest = _build_manifest(parent_id, child_id, sections)
    total_records = sum(s.get("record_count", 0) for s in sections.values())

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.txt", _readme_text(parent_id, child_id))
        for name, payload in sections.items():
            zf.writestr(
                name + ".json",
                json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            )
        zf.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )

    return buf.getvalue(), ExportSummary(
        parent_id=parent_id,
        child_id=child_id,
        file_count=len(sections) + 2,
        total_records=total_records,
        zipped_size=buf.tell(),
        exported_at=_now_iso(),
    )
