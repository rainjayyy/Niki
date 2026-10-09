"""Security primitives (BE-AUTH-001-rollout-alerts).

Currently:
    alerts           — SecurityAlert + AlertDispatcher + sinks
    rate_tracker     — ViolationRateTracker sliding window
"""

from niki.security.alerts import (
    AlertDispatcher,
    CompositeAlertDispatcher,
    ConsoleAlertDispatcher,
    SEVERITY_CRITICAL,
    SEVERITY_WARNING,
    SecurityAlert,
    WebhookAlertDispatcher,
    get_alert_dispatcher,
    set_alert_dispatcher,
)
from niki.security.rate_tracker import ViolationRateTracker

__all__ = [
    "AlertDispatcher",
    "CompositeAlertDispatcher",
    "ConsoleAlertDispatcher",
    "SEVERITY_CRITICAL",
    "SEVERITY_WARNING",
    "SecurityAlert",
    "ViolationRateTracker",
    "WebhookAlertDispatcher",
    "get_alert_dispatcher",
    "set_alert_dispatcher",
]
