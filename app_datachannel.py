import logging
logger = logging.getLogger("rtspwebrtc.datachannel")
import asyncio
import contextlib
import json
import struct
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, SupportsBytes, cast

import av
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaPlayer
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).parent
STATIC_DIR = ROOT / "static"

app = FastAPI(title="RTSP -> WebRTC DataChannel (WebCodecs)")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")



@dataclass
class Session:
    pc: RTCPeerConnection
    player: MediaPlayer
    dc_task: Optional[asyncio.Task] = None
    dc_state: str = "waiting"
    data_channel: Optional[Any] = None


sessions: Dict[str, Session] = {}


class OfferRequest(BaseModel):
    rtspUrl: str = Field(min_length=8)
    sdp: str
    type: str


class StopRequest(BaseModel):
    sessionId: str


def profile_level_to_codec_string(profile_level_id: Optional[str]) -> str:
    value = (profile_level_id or "42e01f").lower()
    return f"avc1.{value}" if len(value) == 6 else "avc1.42e01f"


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
            s = start + prefix_len
            e = starts[idx + 1] if idx + 1 < len(starts) else end
            n = payload[s:e]
            if n:
                nalus.append(n)
        return nalus

    i = 0
    end = len(payload)
    while i + 4 <= end:
        nalu_len = struct.unpack("!I", payload[i : i + 4])[0]
        i += 4
        if nalu_len <= 0 or i + nalu_len > end:
            break
        n = payload[i : i + nalu_len]
        if n:
            nalus.append(n)
        i += nalu_len
    return nalus


def extract_nal_types(payload: bytes) -> list[int]:
    nal_types: list[int] = []

    if b"\x00\x00\x01" in payload or b"\x00\x00\x00\x01" in payload:
        i = 0
        end = len(payload)
        while i + 4 <= end:
            if payload[i : i + 3] == b"\x00\x00\x01":
                start = i + 3
            elif payload[i : i + 4] == b"\x00\x00\x00\x01":
                start = i + 4
            else:
                i += 1
                continue
            if start < end:
                nal_types.append(payload[start] & 0x1F)
            i = start + 1
        return nal_types

    i = 0
    end = len(payload)
    while i + 4 <= end:
        nalu_len = struct.unpack("!I", payload[i : i + 4])[0]
        i += 4
        if nalu_len <= 0 or i + nalu_len > end:
            break
        nal_types.append(payload[i] & 0x1F)
        i += nalu_len

    return nal_types


def to_annexb_payload(payload: bytes) -> bytes:
    nalus = extract_nalus(payload)
    if not nalus:
        return payload
    return b"".join(b"\x00\x00\x00\x01" + n for n in nalus)


def parse_h264_config_from_extradata(extradata: bytes) -> tuple[bytes, Optional[str]]:
    if not extradata:
        return b"", None

    profile_level_id: Optional[str] = None
    sps_nalus: list[bytes] = []
    pps_nalus: list[bytes] = []

    if len(extradata) >= 7 and extradata[0] == 1:
        profile_level_id = f"{extradata[1]:02x}{extradata[2]:02x}{extradata[3]:02x}"
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
                nalu = extradata[offset : offset + nalu_len]
                offset += nalu_len
                if nalu:
                    sps_nalus.append(nalu)
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
                    nalu = extradata[offset : offset + nalu_len]
                    offset += nalu_len
                    if nalu:
                        pps_nalus.append(nalu)
    else:
        for nalu in extract_nalus(extradata):
            if not nalu:
                continue
            ntype = nalu[0] & 0x1F
            if ntype == 7:
                sps_nalus.append(nalu)
            elif ntype == 8:
                pps_nalus.append(nalu)

    if profile_level_id is None and sps_nalus:
        first_sps = sps_nalus[0]
        if len(first_sps) >= 4:
            profile_level_id = f"{first_sps[1]:02x}{first_sps[2]:02x}{first_sps[3]:02x}"

    prefix = b"".join(b"\x00\x00\x00\x01" + n for n in (sps_nalus + pps_nalus) if n)
    return prefix, profile_level_id


def parse_prefix_and_profile(rtsp_url: str, options: Dict[str, str]) -> tuple[bytes, Optional[str]]:
    prefix = b""
    profile_level_id: Optional[str] = None

    with av.open(rtsp_url, format="rtsp", options=options) as c:
        vs = next((s for s in c.streams if s.type == "video"), None)
        if not vs or not vs.codec_context:
            return prefix, profile_level_id

        extradata = bytes(vs.codec_context.extradata) if vs.codec_context.extradata else b""
        prefix, profile_level_id = parse_h264_config_from_extradata(extradata)

    return prefix, profile_level_id


async def stream_datachannel(session_id: str, track: MediaStreamTrack, dc: Any, prefix: bytes, profile_level_id: Optional[str]):
    logger.info(f"stream_datachannel START session_id={session_id} profile_level_id={profile_level_id}")
    frame_id = 0
    max_chunk = 14000

    try:
        dc.send(json.dumps({
            "type": "init",
            "codec": profile_level_to_codec_string(profile_level_id),
            "profileLevelId": profile_level_id,
            "maxChunkSize": max_chunk,
        }))
        logger.info(f"DataChannel init sent session_id={session_id} codec={profile_level_to_codec_string(profile_level_id)}")
    except Exception as exc:
        logger.error(f"DataChannel init send failed: {exc}")

    try:
        while True:
            session = sessions.get(session_id)
            if not session:
                logger.warning(f"Session {session_id} not found, terminating stream_datachannel.")
                return
            if dc.readyState != "open":
                logger.debug(f"DataChannel not open, session_id={session_id}, waiting...")
                await asyncio.sleep(0.05)
                continue

            try:
                item = await track.recv()
                logger.debug(f"track.recv() success session_id={session_id} frame_id={frame_id} type={type(item)}")
            except Exception as exc:
                logger.error(f"track.recv() failed: {exc}")
                continue

            payload = None
            try:
                if isinstance(item, av.Packet):
                    payload = bytes(item)
                elif hasattr(item, "to_bytes") and callable(getattr(item, "to_bytes")):
                    payload = getattr(item, "to_bytes")()
                elif hasattr(item, "__bytes__"):
                    payload = bytes(cast(SupportsBytes, item))
                logger.debug(f"Payload extracted session_id={session_id} frame_id={frame_id} payload_len={len(payload) if payload else 0}")
            except Exception as exc:
                logger.error(f"Payload extraction failed: {exc} type={type(item)}")
                payload = None

            if not payload:
                logger.warning(f"No payload extracted for frame_id={frame_id}")
                continue

            try:
                payload = to_annexb_payload(payload)
                nal_types = extract_nal_types(payload)
                has_idr = 5 in nal_types
                has_sps = 7 in nal_types
                has_pps = 8 in nal_types
                is_key = frame_id == 0 or has_idr or bool(getattr(item, "is_keyframe", False))
                logger.info(f"frame_id={frame_id} NAL types={nal_types} is_key={is_key} has_sps={has_sps} has_pps={has_pps} has_idr={has_idr}")
            except Exception as exc:
                logger.error(f"NAL type extraction failed: {exc}")
                is_key = frame_id == 0

            try:
                if prefix and (frame_id == 0 or (is_key and not (has_sps and has_pps))):
                    logger.info(f"Injecting SPS/PPS prefix for frame_id={frame_id} is_key={is_key} has_sps={has_sps} has_pps={has_pps}")
                    payload = prefix + payload
            except Exception as exc:
                logger.error(f"Prefix injection failed: {exc}")

            pts = getattr(item, "pts", None)
            time_base = getattr(item, "time_base", None)
            try:
                timestamp_us = int(float(pts * time_base) * 1_000_000) if pts is not None and time_base is not None else int(time.time() * 1_000_000)
            except Exception as exc:
                logger.error(f"Timestamp calculation failed: {exc}")
                timestamp_us = int(time.time() * 1_000_000)

            chunks = max(1, (len(payload) + max_chunk - 1) // max_chunk)
            logger.info(f"Sending frame_id={frame_id} chunks={chunks} is_key={is_key} payload_len={len(payload)}")
            for idx in range(chunks):
                s = idx * max_chunk
                e = min(len(payload), s + max_chunk)
                data = payload[s:e]
                header = struct.pack("!B I H H B Q", 1, frame_id, idx, chunks, 1 if is_key else 0, timestamp_us)
                try:
                    dc.send(header + data)
                    logger.debug(f"DataChannel send success: frame_id={frame_id} chunk={idx}/{chunks} data_len={len(data)}")
                except Exception as exc:
                    logger.error(f"DataChannel send failed: frame_id={frame_id} chunk={idx}/{chunks} error={exc}")

            frame_id += 1
    except asyncio.CancelledError:
        logger.info(f"DataChannel stream cancelled session_id={session_id}")
        raise
    except Exception as exc:
        logger.exception(f"DataChannel stream failed session_id={session_id}: {exc}")
    logger.info(f"stream_datachannel END session_id={session_id}")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index_datachannel.html")


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
        player = MediaPlayer(rtsp_url, format="rtsp", options=options, decode=False)
    except Exception as exc:
        await pc.close()
        raise HTTPException(status_code=400, detail=f"failed to open rtsp stream: {exc}") from exc

    if not player.video:
        await pc.close()
        raise HTTPException(status_code=400, detail="no video track found in rtsp stream")

    prefix = b""
    profile = None
    try:
        prefix, profile = parse_prefix_and_profile(rtsp_url, options)
    except Exception:
        pass

    session_id = str(uuid.uuid4())
    video_track = player.video

    sessions[session_id] = Session(pc=pc, player=player)


    @pc.on("datachannel")
    def on_datachannel(channel):
        logger.info("DataChannel received session_id=%s label=%s", session_id, getattr(channel, "label", ""))
        session = sessions.get(session_id)
        if not session:
            return

        session.data_channel = channel
        session.dc_state = channel.readyState

        def start_dc_stream_if_needed():
            if session.dc_task is None:
                session.dc_task = asyncio.create_task(
                    stream_datachannel(
                        session_id,
                        video_track,
                        channel,
                        prefix,
                        profile
                    )
                )

        @channel.on("open")
        def on_dc_open():
            logger.info("DataChannel open session_id=%s", session_id)
            session.dc_state = channel.readyState
            start_dc_stream_if_needed()

        @channel.on("close")
        def on_dc_close():
            logger.info("DataChannel close session_id=%s", session_id)
            session.dc_state = channel.readyState

        @channel.on("error")
        def on_dc_error(error):
            logger.warning("DataChannel error session_id=%s error=%s", session_id, error)

        @channel.on("message")
        def on_dc_message(message):
            if isinstance(message, str):
                logger.info("DataChannel message session_id=%s message=%s", session_id, message)

        if channel.readyState == "open":
            start_dc_stream_if_needed()

    @pc.on("connectionstatechange")
    async def on_state_change():
        if pc.connectionState in {"failed", "closed", "disconnected"}:
            await close_session(session_id)

    try:
        await pc.setRemoteDescription(RTCSessionDescription(sdp=body.sdp, type=body.type))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
    except Exception as exc:
        await pc.close()
        raise HTTPException(status_code=400, detail=f"webrtc negotiation failed: {exc}") from exc

    return {
        "sessionId": session_id,
        "sdp": pc.localDescription.sdp,
        "type": pc.localDescription.type,
        "mode": "datachannel",
        "profileLevelId": profile,
        "prefixBytes": len(prefix),
    }


@app.post("/api/stop")
async def stop_stream(body: StopRequest):
    return {"ok": await close_session(body.sessionId)}


@app.get("/api/health")
async def health():
    return {"ok": True, "sessions": len(sessions), "mode": "datachannel"}


async def close_session(session_id: str) -> bool:
    session = sessions.pop(session_id, None)
    if not session:
        return False

    if session.dc_task:
        session.dc_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await session.dc_task

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
