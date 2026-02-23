import contextlib
import re
import struct
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import CoroutineType
from typing import Any, Dict

import av
from av.frame import Frame
from av.packet import Packet
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCRtpSender, RTCSessionDescription
from aiortc.contrib.media import MediaPlayer
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).parent
STATIC_DIR = ROOT / "static"

app = FastAPI(title="RTSP -> WebRTC (Passthrough)")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@dataclass
class Session:
    pc: RTCPeerConnection
    player: MediaPlayer


sessions: Dict[str, Session] = {}
FORCE_PROFILE_LEVEL_ID = "64001f"


class OfferRequest(BaseModel):
    rtspUrl: str = Field(min_length=8)
    sdp: str
    type: str
    preferPassThrough: bool = True
    transportMode: str = "rtp"


class StopRequest(BaseModel):
    sessionId: str


def extract_nalus(payload: bytes) -> list[bytes]:
    nalus: list[bytes] = []
    if b"\x00\x00\x01" in payload or b"\x00\x00\x00\x01" in payload:
        i = 0
        end = len(payload)
        starts: list[int] = []
        while i + 3 < end:
            if payload[i : i + 3] == b"\x00\x00\x01":
                starts.append(i)
                i += 3
            elif i + 4 <= end and payload[i : i + 4] == b"\x00\x00\x00\x01":
                starts.append(i)
                i += 4
            else:
                i += 1
        for idx, start in enumerate(starts):
            prefix_len = 4 if payload[start : start + 4] == b"\x00\x00\x00\x01" else 3
            nalu_start = start + prefix_len
            nalu_end = starts[idx + 1] if idx + 1 < len(starts) else end
            nalu = payload[nalu_start:nalu_end]
            if nalu:
                nalus.append(nalu)
        return nalus

    i = 0
    end = len(payload)
    while i + 4 <= end:
        nalu_len = struct.unpack("!I", payload[i : i + 4])[0]
        i += 4
        if nalu_len <= 0 or i + nalu_len > end:
            break
        nalu = payload[i : i + nalu_len]
        if nalu:
            nalus.append(nalu)
        i += nalu_len
    return nalus


def is_annexb(payload: bytes) -> bool:
    return b"\x00\x00\x01" in payload or b"\x00\x00\x00\x01" in payload


def rewrite_sps_level_in_nalu(nalu: bytes, target_level_idc: int) -> bytes:
    if not nalu:
        return nalu
    if (nalu[0] & 0x1F) != 7:
        return nalu
    if len(nalu) < 4:
        return nalu

    rewritten = bytearray(nalu)
    rewritten[3] = target_level_idc & 0xFF
    return bytes(rewritten)


def rewrite_sps_level_in_payload(payload: bytes, target_level_idc: int) -> bytes:
    nalus = extract_nalus(payload)
    if not nalus:
        return payload

    rewritten_nalus = [rewrite_sps_level_in_nalu(nalu, target_level_idc) for nalu in nalus]

    if is_annexb(payload):
        return b"".join(b"\x00\x00\x00\x01" + n for n in rewritten_nalus)

    out = bytearray()
    for n in rewritten_nalus:
        out.extend(struct.pack("!I", len(n)))
        out.extend(n)
    return bytes(out)


def rewrite_profile_level_id_in_sdp(sdp: str, profile_level_id: str) -> str:
    return re.sub(
        r"profile-level-id=[0-9a-fA-F]{6}",
        f"profile-level-id={profile_level_id.lower()}",
        sdp,
        flags=re.IGNORECASE,
    )


def offer_has_h264(sdp: str) -> bool:
    return bool(re.search(r"a=rtpmap:\\d+\\s+H264/90000", sdp, flags=re.IGNORECASE))


def parse_h264_annexb_prefix(extradata: bytes) -> bytes:
    if not extradata:
        return b""

    sps_pps: list[bytes] = []
    if len(extradata) >= 7 and extradata[0] == 1:
        offset = 5
        if offset < len(extradata):
            num_sps = extradata[offset] & 0x1F
            offset += 1
            for _ in range(num_sps):
                if offset + 2 > len(extradata):
                    break
                nalu_len = (extradata[offset] << 8) | extradata[offset + 1]
                offset += 2
                if offset + nalu_len > len(extradata):
                    break
                sps_pps.append(extradata[offset : offset + nalu_len])
                offset += nalu_len
            if offset < len(extradata):
                num_pps = extradata[offset]
                offset += 1
                for _ in range(num_pps):
                    if offset + 2 > len(extradata):
                        break
                    nalu_len = (extradata[offset] << 8) | extradata[offset + 1]
                    offset += 2
                    if offset + nalu_len > len(extradata):
                        break
                    sps_pps.append(extradata[offset : offset + nalu_len])
                    offset += nalu_len
    else:
        for nalu in extract_nalus(extradata):
            if nalu and (nalu[0] & 0x1F) in {7, 8}:
                sps_pps.append(nalu)

    return b"".join(b"\x00\x00\x00\x01" + n for n in sps_pps if n)


class PrefixInjectTrack(MediaStreamTrack):
    kind = "video"

    def __init__(self, source: MediaStreamTrack, prefix: bytes, target_level_idc: int):
        super().__init__()
        self.source = source
        self.prefix = prefix
        self.injected = False
        self.target_level_idc = target_level_idc

    def recv(self) -> CoroutineType[Any, Any, Frame | Packet]:
        return self._recv_impl()

    async def _recv_impl(self) -> Frame | Packet:
        item = await self.source.recv()
        if not isinstance(item, av.Packet):
            return item

        payload = bytes(item)
        payload = rewrite_sps_level_in_payload(payload, self.target_level_idc)
        if not payload or not self.prefix:
            patched = av.Packet(payload)
            patched.pts = item.pts
            patched.dts = item.dts
            if item.time_base is not None:
                patched.time_base = item.time_base
            return patched

        nalus = extract_nalus(payload)
        has_sps = any((n[0] & 0x1F) == 7 for n in nalus if n)
        has_pps = any((n[0] & 0x1F) == 8 for n in nalus if n)

        if not self.injected or not (has_sps and has_pps):
            merged = self.prefix + payload
            patched = av.Packet(merged)
            patched.pts = item.pts
            patched.dts = item.dts
            if item.time_base is not None:
                patched.time_base = item.time_base
            self.injected = True
            return patched

        return item


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index_passthrough.html")


@app.post("/api/offer")
async def create_offer(body: OfferRequest):
    rtsp_url = body.rtspUrl.strip()

    if (body.transportMode or "rtp").strip().lower() != "rtp":
        raise HTTPException(status_code=400, detail="app_passthrough는 RTP 패스스루 전용입니다")

    if not body.preferPassThrough:
        raise HTTPException(status_code=400, detail="app_passthrough는 패스스루 전용입니다")

    if not (rtsp_url.startswith("rtsp://") or rtsp_url.startswith("rtsps://")):
        raise HTTPException(status_code=400, detail="rtspUrl must start with rtsp:// or rtsps://")

    if not offer_has_h264(body.sdp):
        raise HTTPException(status_code=400, detail="browser offer does not include H264 (pass-through requires H264)")

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
        player = MediaPlayer(rtsp_url, format="rtsp", options=options, decode=False)
    except Exception as exc:
        await pc.close()
        raise HTTPException(status_code=400, detail=f"failed to open rtsp stream: {exc}") from exc

    if not player.video:
        await pc.close()
        raise HTTPException(status_code=400, detail="no video track found in rtsp stream")

    prefix = b""
    target_level_idc = int(FORCE_PROFILE_LEVEL_ID[-2:], 16)
    try:
        with av.open(rtsp_url, format="rtsp", options=options) as c:
            vs = next((s for s in c.streams if s.type == "video"), None)
            if vs and vs.codec_context and vs.codec_context.extradata:
                prefix = parse_h264_annexb_prefix(bytes(vs.codec_context.extradata))
                prefix = rewrite_sps_level_in_payload(prefix, target_level_idc)
    except Exception:
        pass

    track = PrefixInjectTrack(player.video, prefix, target_level_idc)
    sender = pc.addTrack(track)
    session_id = str(uuid.uuid4())

    @pc.on("connectionstatechange")
    async def on_state_change():
        if pc.connectionState in {"failed", "closed", "disconnected"}:
            await close_session(session_id)

    try:
        await pc.setRemoteDescription(RTCSessionDescription(sdp=body.sdp, type=body.type))

        transceiver = next((t for t in pc.getTransceivers() if t.sender == sender), None)
        if transceiver is None:
            raise RuntimeError("failed to find video transceiver for H264 codec forcing")

        caps = RTCRtpSender.getCapabilities("video")
        h264_codecs = [codec for codec in caps.codecs if codec.mimeType == "video/H264"]
        if not h264_codecs:
            raise RuntimeError("server video capabilities do not include H264")

        transceiver.setCodecPreferences(h264_codecs)

        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
    except Exception as exc:
        await pc.close()
        raise HTTPException(status_code=400, detail=f"webrtc negotiation failed: {exc}") from exc

    sessions[session_id] = Session(pc=pc, player=player)

    return {
        "sessionId": session_id,
        "sdp": rewrite_profile_level_id_in_sdp(pc.localDescription.sdp, FORCE_PROFILE_LEVEL_ID),
        "type": pc.localDescription.type,
        "mode": "passthrough",
        "prefixBytes": len(prefix),
        "forcedProfileLevelId": FORCE_PROFILE_LEVEL_ID,
    }


@app.post("/api/stop")
async def stop_stream(body: StopRequest):
    return {"ok": await close_session(body.sessionId)}


@app.get("/api/health")
async def health():
    return {"ok": True, "sessions": len(sessions), "mode": "passthrough"}


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
