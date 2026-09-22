"""Vercel entry point.

Vercel's Python runtime looks for a module under api/ exporting an ASGI `app`.
Keeping this file separate from api/app.py means the application is importable
and testable without anything Vercel-specific in the way.
"""

from .app import app  # noqa: F401
