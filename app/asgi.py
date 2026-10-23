"""ASGI entrypoint: ``uvicorn app.asgi:app``. Kept separate from the factory so importing
``app.main`` never boots a server app as a side effect (tests build apps with explicit Settings)."""

from __future__ import annotations

from app.main import create_app

app = create_app()
