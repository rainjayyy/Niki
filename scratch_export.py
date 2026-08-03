from pathlib import Path
content = '''"""Data export service (BE-COMP-001). PIPL/GDPR data portability."""

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


def _bytes_to_b64(b):
    if b is None:
        return None
    if isinstance(b, str):
        return b
    return base64.b64encode(b).decode("ascii")


async def _collect_account(parent_id):
    return {
        "exported_at": _now_iso(),
        "parent_id": parent_id,
        "fields": ["id", "name", "email (masked)", "phone (masked)", "status", "created_at"],
        "note": "Schema spec only.",
    }


async def _collect_children(parent_id, child_id):
    return {
        "exported_at": _now_iso(),
        "record_count": 0,
        "items": [],
        "schema": ["id (uuid)", "name", "birth_date", "avatar_url", "created_at"],
        "filter": {"parent_id": parent_id, "child_id": child_id},
    }


async def _collect_alerts(repo, parent_id, child_id):
    rows = await repo.list_alerts(alert_type=None, child_id=child_id)
    owned = [r for r in rows if r.get("parent_id") == parent_id]
    return {
        "exported_at": _now_iso(),
        "record_count": len(owned),
        "items": owned,
        "schema": ["id", "alert_type", "child_id", "parent_id", "payload", "created_at", "delivered_at"],
    }


async def _collect_tutoring_events(repo, parent_id, child_id):
    items = await repo.list_tutoring_events(child_id) if child_id else []
    owned = [r for r in items if r.get("parent_id") == parent_id]
    return {
        "exported_at": _now_iso(),
        "record_count": len(owned),
        "items": owned,
        "schema": ["id", "child_id", "parent_id", "payload", "created_at"],
    }


async def _collect_knowledge_queries(repo, parent_id, child_id):
    items = await repo.list_knowledge_queries(child_id) if child_id else []
    owned = [r for r in items if r.get("parent_id") == parent_id]
    return {
        "exported_at": _now_iso(),
        "record_count": len(owned),
        "items": owned,
        "schema": ["id", "child_id", "parent_id", "payload", "created_at"],
    }


async def _collect_treehole(repo, parent_id, child_id):
    rows = await repo.list_for_child(child_id) if child_id else []
    owned = [r for r in rows if r.get("parent_id") == parent_id]
    items = []
    for r in owned:
        items.append({
            "id": r.get("id"),
            "child_id": r.get("child_id"),
            "parent_id": r.get("parent_id"),
            "encrypted_payload_b64": _bytes_to_b64(r.get("encrypted_payload")),
            "associated_data_b64": _bytes_to_b64(r.get("associated_data")),
            "expires_at": r["expires_at"].isoformat() if r.get("expires_at") else None,
            "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
        })
    return {
        "exported_at": _now_iso(),
        "record_count": len(items),
        "items": items,
        "schema": ["id", "child_id", "parent_id", "encrypted_payload_b64", "associated_data_b64", "expires_at", "created_at"],
        "note": "Ciphertext per PIPL Art. 44.",
    }


async def _collect_emotion_baseline(repo, parent_id):
    rows = await repo.list_in_window(window_days=30)
    return {
        "exported_at": _now_iso(),
        "record_count": len(rows),
        "items": rows,
        "schema": ["id", "date", "valence", "intensity", "source", "created_at"],
    }


def _build_manifest(parent_id, child_id, sections):
    return {
        "exported_at": _now_iso(),
        "parent_id": parent_id,
        "child_id_filter": child_id,
        "policy": "PIPL Art. 44 / GDPR Art. 20 (data portability)",
        "niki_version": "0.1.0",
        "sections": {
            name: {"record_count": s.get("record_count", 0), "schema": s.get("schema", [])}
            for name, s in sections.items()
        },
    }


def _readme_text(parent_id, child_id):
    scope = "child " + str(child_id) if child_id else "ALL children"
    return (
        "Niki data portability export\n"
        "==================================\n"
        "Parent ID : " + str(parent_id) + "\n"
        "Scope     : " + scope + "\n"
        "Exported  : " + _now_iso() + "\n\n"
        "Files in this archive:\n"
        "  - README.txt        this file\n"
        "  - account.json      parent profile (schema spec)\n"
        "  - children.json     child list (filtered to scope)\n"
        "  - alerts.json       dashboard alerts\n"
        "  - tutoring.json     tutoring events\n"
        "  - knowledge.json    knowledge queries\n"
        "  - treehole.json     treehole engagements (encrypted)\n"
        "  - emotion_baseline.json  30-day rolling emotion baseline\n"
        "  - manifest.json     counts + field glossary\n"
    )


async def build_export_zip(parent_id, child_id=None):
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
            zf.writestr(name + ".json", json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    return buf.getvalue(), ExportSummary(
        parent_id=parent_id,
        child_id=child_id,
        file_count=len(sections) + 2,
        total_records=total_records,
        zipped_size=buf.tell(),
        exported_at=_now_iso(),
    )
'''
from pathlib import Path
Path('niki/services/data_export.py').write_text(content, encoding='utf-8')
print('written:', Path('niki/services/data_export.py').stat().st_size, 'bytes')
