"""Pytest rootdir conftest — puts the repo root on sys.path so tests can
`import db`, `import cogs.onboarding`, etc. the same way the bot does when
launched with `python bot.py` from the repo root."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
