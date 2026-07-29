"""ORM models for the Niki persistence layer (BE-STOR-001).

Importing this package registers every model with `Base.metadata`,
which Alembic depends on for autogenerate.

Tables:
    Parent — 家长账号
    Child — 儿童档案
    AccountVerification — 注册 3 步进度（real_name / face / consent）
    DeletionRequest — 30 天冷静期内的删除请求
    WeeklyReport — 家长端周报
    EmotionBaseline — 儿童情绪基线
    MemoryChunk — 长时记忆片段
    TreeholeSummary — 心洞模式加密聚合
"""

from __future__ import annotations

from niki.db.models.account_verification import AccountVerification
from niki.db.models.auth_audit_log import AuthAuditLog
from niki.db.models.child import Child
from niki.db.models.dashboard_record import (
    DashboardAlert,
    KnowledgeQuery,
    TutoringEvent,
)
from niki.db.models.deletion_request import DeletionRequest
from niki.db.models.emotion_baseline_record import EmotionBaselineRecord
from niki.db.models.memory_chunk import MemoryChunk
from niki.db.models.parent import Parent, ParentStatus
from niki.db.models.treehole_engagement import TreeholeEngagement
from niki.db.models.treehole_aggregation import TreeholeAggregation
from niki.db.models.treehole_summary import TreeholeSummary
from niki.db.models.weekly_report import WeeklyReport

__all__ = [
    "Parent",
    "ParentStatus",
    "Child",
    "AccountVerification",
    "DeletionRequest",
    "WeeklyReport",
    "EmotionBaselineRecord",
    "MemoryChunk",
    "TreeholeSummary",
    "TreeholeEngagement",
    "TreeholeAggregation",
    "DashboardAlert",
    "TutoringEvent",
    "KnowledgeQuery",
]
