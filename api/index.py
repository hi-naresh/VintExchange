"""Vercel entrypoint: serves the whole FastAPI app (API + dashboard).

Vercel's filesystem is read-only except /tmp, so the demo SQLite database lives
there (set DATABASE_URL=sqlite+aiosqlite:////tmp/tac.db). Each serverless
instance seeds its own copy on first request, so treat Vercel as a preview link;
use Railway (or PROFILE=connected with Supabase) for shared, durable state.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:////tmp/tac.db")

from app.main import app  # noqa: E402

__all__ = ["app"]
