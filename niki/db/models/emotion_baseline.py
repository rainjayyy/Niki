"""DEPRECATED: the canonical EmotionBaselineRecord lives in
``emotion_baseline_record.py`` (the one imported by
``niki.db.models.__init__``). This file is retained only as a stub
so older imports that referenced ``niki.db.models.emotion_baseline``
keep resolving during the deprecation window.

The schema difference from the canonical model — the ``child_id``
column was added in alembic revision 2026_10_10_0001 — is NOT
reflected here; do NOT import from this file.
"""

from __future__ import annotations

from niki.db.models.emotion_baseline_record import EmotionBaselineRecord  # noqa: F401

__all__ = ["EmotionBaselineRecord"]
