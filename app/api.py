"""Pipeline HTTP service.

The WhatsApp bot is Node/Express; the verification pipeline is Python. Rather
than spawn Python per message (cold-start cost on every request), we run the
pipeline as a small long-lived FastAPI service and let the Node bot POST to it.

    uvicorn app.api:app --host 127.0.0.1 --port 8000

Endpoints:
    GET  /health   -> liveness
    POST /process  -> {text, language?, with_queries?} -> full pipeline result
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel, Field

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.pipeline import process_message  # noqa: E402

app = FastAPI(title="ECHO pipeline", version="0.1.0")


class ProcessRequest(BaseModel):
    text: str = Field(..., description="The forwarded message text to verify.")
    language: str | None = Field(None, description="Worker's chosen reply language (BCP-47), for later localisation.")
    with_verify: bool = Field(True, description="Run verdict + citation audit + gates for each claim (stages 7-9).")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/process")
def process(req: ProcessRequest) -> dict:
    result = process_message(req.text, with_verify=req.with_verify)
    payload = result.to_dict()
    payload["reply_language"] = req.language
    return payload
