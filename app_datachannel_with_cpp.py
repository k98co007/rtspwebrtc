import logging
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

logger = logging.getLogger("rtspwebrtc.datachannel_with_cpp")
logging.basicConfig(level=logging.DEBUG)
logger.setLevel(logging.DEBUG)

ROOT = Path(__file__).parent
STATIC_DIR = ROOT / "static"

app = FastAPI(title="RTSP -> WebRTC DataChannel (C++ backend only)")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Require the C++ extension (pybind11 built module).
try:
    import rtspwebrtc_cpp  # type: ignore
    logger.info("rtspwebrtc_cpp extension loaded")
except Exception as exc:
    logger.exception("rtspwebrtc_cpp extension import failed; this application requires the C++ extension")
    raise ImportError("rtspwebrtc_cpp extension is required") from exc


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index_datachannel.html")


class OfferRequest(BaseModel):
    rtspUrl: str = Field(min_length=8)
    sdp: str
    type: str


class StopRequest(BaseModel):
    sessionId: str


@app.post("/api/offer")
async def create_offer(body: OfferRequest):
    rtsp_url = body.rtspUrl.strip()
    logger.info("create_offer called: rtspUrl=%s sdp_len=%d type=%s", rtsp_url, len(body.sdp or ""), body.type)
    if not (rtsp_url.startswith("rtsp://") or rtsp_url.startswith("rtsps://")):
        raise HTTPException(status_code=400, detail="rtspUrl must start with rtsp:// or rtsps://")

    try:
        logger.debug("Calling rtspwebrtc_cpp.create_session(rtsp_url, sdp...)")
        raw = cast(Any, rtspwebrtc_cpp).create_session(rtsp_url, body.sdp)
        logger.info("C++ create_session returned (raw): %s", raw)

        # Normalize result (C++ extension may return dict-like or an object)
        res = {}
        if isinstance(raw, dict):
            res = raw
        else:
            # Try attribute access
            res = {
                "sessionId": getattr(raw, "sessionId", None) or getattr(raw, "session_id", None),
                "sdp": getattr(raw, "sdp", None),
                "type": getattr(raw, "type", None),
                "profileLevelId": getattr(raw, "profileLevelId", None) or getattr(raw, "profile_level_id", None),
            }

        logger.info("C++ create_session normalized result: %s", res)

        if not res or not res.get("sessionId") or not res.get("sdp") or not res.get("type"):
            raise ValueError("create_session returned missing sessionId/sdp/type")

        return res
    except Exception as exc:
        logger.exception("C++ create_session failed")
        raise HTTPException(status_code=500, detail=f"cpp create_session failed: {exc}") from exc


@app.post("/api/stop")
async def stop_stream(body: StopRequest):
    logger.info("stop_stream called: sessionId=%s", body.sessionId)
    try:
        ok = cast(Any, rtspwebrtc_cpp).stop_session(body.sessionId)
        logger.info("C++ stop_session returned: %s for sessionId=%s", ok, body.sessionId)
        return {"ok": bool(ok)}
    except Exception as exc:
        logger.exception("C++ stop_session failed")
        raise HTTPException(status_code=500, detail=f"cpp stop_session failed: {exc}") from exc


@app.get("/api/health")
async def health():
    try:
        if hasattr(rtspwebrtc_cpp, "health"):
            return cast(Any, rtspwebrtc_cpp).health()
        logger.info("rtspwebrtc_cpp.health not present; returning basic health info")
        return {"ok": True, "mode": "cpp", "sessions": 0}
    except Exception:
        logger.exception("C++ health() failed")
        raise HTTPException(status_code=500, detail="cpp health() failed")


@app.on_event("shutdown")
async def on_shutdown():
    try:
        if hasattr(rtspwebrtc_cpp, "shutdown_all"):
            cast(Any, rtspwebrtc_cpp).shutdown_all()
        else:
            logger.info("rtspwebrtc_cpp.shutdown_all not present; skipping C++ shutdown")
    except Exception:
        logger.exception("rtspwebrtc_cpp.shutdown_all failed")
