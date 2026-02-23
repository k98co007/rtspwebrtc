import contextlib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict

from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaPlayer
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).parent
STATIC_DIR = ROOT / "static"

app = FastAPI(title="RTSP -> WebRTC (Transcoding)")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@dataclass
class Session:
    pc: RTCPeerConnection
    player: MediaPlayer


sessions: Dict[str, Session] = {}


class OfferRequest(BaseModel):
    rtspUrl: str = Field(min_length=8)
    sdp: str
    type: str


class StopRequest(BaseModel):
    sessionId: str


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index_transcoding.html")


@app.post("/api/offer")
async def create_offer(body: OfferRequest):
    rtsp_url = body.rtspUrl.strip()
    if not (rtsp_url.startswith("rtsp://") or rtsp_url.startswith("rtsps://")):
        raise HTTPException(status_code=400, detail="rtspUrl must start with rtsp:// or rtsps://")

    pc = RTCPeerConnection()
    options = {
        "rtsp_transport": "tcp",
        "fflags": "nobuffer",
        "flags": "low_delay",
        "analyzeduration": "0",
        "probesize": "32768",
        "max_delay": "0",
    }

    try:
        player = MediaPlayer(rtsp_url, format="rtsp", options=options, decode=True)
    except Exception as exc:
        await pc.close()
        raise HTTPException(status_code=400, detail=f"failed to open rtsp stream: {exc}") from exc

    if not player.video:
        with contextlib.suppress(Exception):
            if player.audio:
                player.audio.stop()
        await pc.close()
        raise HTTPException(status_code=400, detail="no video track found in rtsp stream")

    pc.addTrack(player.video)
    session_id = str(uuid.uuid4())

    @pc.on("connectionstatechange")
    async def on_state_change():
        if pc.connectionState in {"failed", "closed", "disconnected"}:
            await close_session(session_id)

    try:
        await pc.setRemoteDescription(RTCSessionDescription(sdp=body.sdp, type=body.type))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
    except Exception as exc:
        with contextlib.suppress(Exception):
            if player.video:
                player.video.stop()
        await pc.close()
        raise HTTPException(status_code=400, detail=f"webrtc negotiation failed: {exc}") from exc

    sessions[session_id] = Session(pc=pc, player=player)

    return {
        "sessionId": session_id,
        "sdp": pc.localDescription.sdp,
        "type": pc.localDescription.type,
        "mode": "transcoding",
    }


@app.post("/api/stop")
async def stop_stream(body: StopRequest):
    return {"ok": await close_session(body.sessionId)}


@app.get("/api/health")
async def health():
    return {"ok": True, "sessions": len(sessions), "mode": "transcoding"}


async def close_session(session_id: str) -> bool:
    session = sessions.pop(session_id, None)
    if not session:
        return False

    with contextlib.suppress(Exception):
        if session.player.video:
            session.player.video.stop()
        if session.player.audio:
            session.player.audio.stop()

    with contextlib.suppress(Exception):
        await session.pc.close()

    return True


@app.on_event("shutdown")
async def on_shutdown():
    for sid in list(sessions.keys()):
        await close_session(sid)
