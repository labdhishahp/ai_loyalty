"""Vercel entry point.

Vercel's Python runtime looks for a module under api/ exporting an ASGI `app`.
Keeping this file separate from api/app.py means the application is importable
and testable without anything Vercel-specific in the way.

THE IMPORT IS ABSOLUTE ON PURPOSE. `from .app import app` works when the
runtime imports this as `api.index`, and raises "attempted relative import with
no known parent package" when it loads the file by path instead. Which of those
Vercel does is an implementation detail of the builder, and not one worth
betting a deployment on. The absolute form works under both.
"""

from api.app import app  # noqa: F401
