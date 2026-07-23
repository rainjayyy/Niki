"""Treehole mode — PRD #13, #61-#64.

Public surface:
    TreeholeManager        — orchestrates treehole mode (detect + store + encrypt)
    TreeholeSummary        — encrypted emotion summary record
    TreeholeEncryption     — AES-256-GCM encrypt/decrypt
    TreeholeActivationDetector — keyword-based activation
    TREEHOLE_TRIGGERS      — keyword list (re-exported from emotion)
"""

from niki.treehole.activation import (
    TREEHOLE_TRIGGERS,
    should_activate_treehole,
)
from niki.treehole.encryption import TreeholeEncryption
from niki.treehole.manager import TreeholeManager, TreeholeStore, TreeholeSummary
from niki.treehole.purge import TreeholePurger

__all__ = [
    "TREEHOLE_TRIGGERS",
    "should_activate_treehole",
    "TreeholeEncryption",
    "TreeholeManager",
    "TreeholeSummary",
    "TreeholeStore",
    "TreeholePurger",
]
