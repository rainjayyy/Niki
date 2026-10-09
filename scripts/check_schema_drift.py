"""Schema drift guard for the persistence-critical tables.

`alembic check` also reports cosmetic index renames and legacy table
removals; this guard focuses on the tables/columns that would cause
runtime failures or silent data loss if missing.
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg

REQUIRED = {
    "parents": ["id", "name", "id_number_hash", "phone_hash", "status", "created_at"],
    "children": ["id", "parent_id", "name", "created_at"],
    "account_verifications": ["id", "parent_id", "step_name", "completed", "created_at"],
    "dashboard_alerts": ["id", "parent_id", "child_id", "payload", "read_at"],
    "letters": ["id", "child_id", "parent_id", "content", "read"],
    "child_achievements": ["child_id", "parent_id", "unlocked", "counters"],
    "chat_sessions": ["session_id", "child_id", "parent_id", "title", "messages"],
    "weekly_report_history": ["id", "child_id", "week_start", "week_end", "report"],
    "emotion_baseline_records": ["id", "date", "valence", "intensity", "created_at"],
    "treehole_engagements": ["id", "child_id", "parent_id", "encrypted_payload", "expires_at"],
}


async def main() -> int:
    dsn = os.environ.get(
        "DATABASE_URL",
        "postgresql://niki:nikidev123@localhost:5432/niki",
    )
    conn = await asyncpg.connect(dsn)
    missing: list[str] = []
    try:
        for table, columns in REQUIRED.items():
            rows = await conn.fetch(
                "SELECT column_name FROM information_schema.columns WHERE table_name = $1",
                table,
            )
            existing = {r["column_name"] for r in rows}
            if not existing:
                missing.append(f"table missing: {table}")
                continue
            for col in columns:
                if col not in existing:
                    missing.append(f"{table}.{col} missing")
    finally:
        await conn.close()

    if missing:
        print("SCHEMA DRIFT:")
        for item in missing:
            print(" -", item)
        return 1
    print("schema ok")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
