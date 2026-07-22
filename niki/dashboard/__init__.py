"""Parent dashboard — PRD #24-#29.

Public surface:
    EmotionWeeklyReport    — weekly emotion report (积极优先, 模糊化负向)
    InterestGraph          — top topics + their frequency
    LearningWhitepaper     — new knowledge, hard problems, new vocabulary
    SafetyValveAlert       — tutoring safety-valve trigger events
    HighRiskAlert          — self-harm / crisis alerts
    DashboardService       — orchestrates report generation
"""

from niki.dashboard.alerts import HighRiskAlert, SafetyValveAlert
from niki.dashboard.interest import InterestGraph, InterestTopic
from niki.dashboard.learning import LearningWhitepaper, NewVocabulary
from niki.dashboard.report import EmotionWeeklyReport, WeeklyTrend
from niki.dashboard.service import DashboardService

__all__ = [
    "EmotionWeeklyReport",
    "WeeklyTrend",
    "InterestGraph",
    "InterestTopic",
    "LearningWhitepaper",
    "NewVocabulary",
    "SafetyValveAlert",
    "HighRiskAlert",
    "DashboardService",
]
