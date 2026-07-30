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
    AuditLog — 统一审计日志（BE-COMP-004，跨域只读历史）
"""

from __future__ import annotations

from niki.db.models.account_verification import AccountVerification
from niki.db.models.audit_log import (
    AuditLog,
    EVENT_CONSENT_SIGNED,
    EVENT_DELETION_COMPLETED,
    EVENT_DELETION_REQUESTED,
    EVENT_HIGH_RISK_RECORDED,
    EVENT_LATERAL_AUTH_VIOLATION,
    EVENT_LOGIN_SUCCESS,
    EVENT_SAFETY_BLOCK,
    EVENT_SMS_SENT,
    VALID_SEVERITIES,
)
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
from niki.db.models.push_device import PushDevice, PushPrefs, VALID_CHANNELS, VALID_PLATFORMS
from niki.db.models.treehole_engagement import TreeholeEngagement
from niki.db.models.treehole_aggregation import (
    TreeholeAggregation,
    VALID_WINDOW_KINDS,
    WINDOW_KIND_DAILY,
    WINDOW_KIND_WEEKLY,
)
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
    "WINDOW_KIND_DAILY",
    "WINDOW_KIND_WEEKLY",
    "VALID_WINDOW_KINDS",
    "DashboardAlert",
    "TutoringEvent",
    "KnowledgeQuery",
    "PushDevice",
    "PushPrefs",
    "VALID_CHANNELS",
    "VALID_PLATFORMS",
    # BE-COMP-004: unified cross-domain audit trail
    "AuditLog",
    "VALID_SEVERITIES",
    "EVENT_CONSENT_SIGNED",
    "EVENT_DELETION_REQUESTED",
    "EVENT_DELETION_COMPLETED",
    "EVENT_SAFETY_BLOCK",
    "EVENT_HIGH_RISK_RECORDED",
    "EVENT_LATERAL_AUTH_VIOLATION",
    "EVENT_SMS_SENT",
    "EVENT_LOGIN_SUCCESS",
]
