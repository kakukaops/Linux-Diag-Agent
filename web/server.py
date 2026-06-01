"""FastAPI web server for the diagnostic agent.

Run:
  uvicorn web.server:app --host 0.0.0.0 --port 8000

  or, with auto-reload during development:
  uvicorn web.server:app --reload --host 0.0.0.0 --port 8000

Endpoints:
  GET  /                       — single-page UI (textarea + diagnose button)
  POST /api/diagnose-stream    — body {"raw_input": "..."}; returns SSE
                                 stream of pipeline events.
  GET  /api/health             — liveness probe
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from web.streaming_pipeline import stream_diagnose

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("web.server")

app = FastAPI(title="Linux Diag Agent")

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))


class DiagnoseRequest(BaseModel):
    raw_input: str
    lang: str = "zh"   # 'zh' or 'en'; injects an output-language directive
                       # into the ReAct system prompt


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/diagnose-stream")
async def diagnose_stream(req: DiagnoseRequest) -> StreamingResponse:
    """POST raw_input, stream back SSE events as the pipeline runs."""
    raw_input = (req.raw_input or "").strip()
    if not raw_input:
        return StreamingResponse(
            iter([_sse({"type": "error", "stage": "input",
                        "message": "raw_input is empty"})]),
            media_type="text/event-stream",
        )

    lang = (req.lang or "zh").lower()
    if lang not in ("zh", "en"):
        lang = "zh"

    def event_stream():
        logger.info("diagnose-stream start: input_len=%d lang=%s",
                    len(raw_input), lang)
        for ev in stream_diagnose(raw_input, lang=lang):
            yield _sse(ev)
        yield _sse({"type": "done"})
        logger.info("diagnose-stream done")

    return StreamingResponse(event_stream(),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, default=str, ensure_ascii=False)}\n\n"
