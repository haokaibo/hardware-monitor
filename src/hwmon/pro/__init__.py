"""Pro tier: SQLite history storage and a threshold alert engine.

These modules are optional. The open agent and dashboard work without them;
``hwmon pro`` enables them. They live behind their own package boundary so the
pro build can be compiled/obfuscated without touching the collectors.

Nothing here imports ``cryptography``; that is confined to :mod:`hwmon.release`
and the separate ``licensing/`` tool, which this package does not depend on.
"""

from .alerts import Alert, AlertEngine, Rule
from .history import HistoryStore

__all__ = ["Alert", "AlertEngine", "Rule", "HistoryStore"]
