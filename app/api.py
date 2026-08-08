"""Compatibility entrypoint — prefer `uvicorn app.webhook:app`.

Historically the LLM verification service lived here as `app.api:app`. All routes
(including POST /process) now live on `app.webhook:app` so ASR, vision, translate,
and claim verification share one process on PIPELINE_URL.
"""

from app.webhook import app

__all__ = ["app"]
