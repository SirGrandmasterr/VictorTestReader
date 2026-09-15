"""Core services for TextEnhanceAI.

``__version__`` is the single source of the application version: the window
title, the About dialog, the relay client's User-Agent and the packaged
builds (``packaging/TextEnhanceAI.spec``) all read it from here. Releases are
tagged ``v<version>`` (see ``.github/workflows/build.yml``).
"""

from .diff_engine import build_edit_session, render_reviewed_text
from .models import ChangeHunk, EditSession, ReviewItem

__version__ = "0.13"
REPOSITORY_URL = "https://github.com/SirGrandmasterr/VictorTestReader"
RELEASES_URL = REPOSITORY_URL + "/releases"

__all__ = [
    "RELEASES_URL",
    "REPOSITORY_URL",
    "__version__",
    "ChangeHunk",
    "EditSession",
    "ReviewItem",
    "build_edit_session",
    "render_reviewed_text",
]
