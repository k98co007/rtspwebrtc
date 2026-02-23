import asyncio
import contextlib
import json
import logging
import os
import re
import struct
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import CoroutineType
from typing import Any, Dict, Optional, SupportsBytes, cast

import av
from av.frame import Frame
from av.packet import Packet
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCRtpSender, RTCSessionDescription
from aiortc.contrib.media import MediaPlayer
from fastapi import FastAPI, HTTPException
from fastapi import Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


ROOT = Path(__file__).parent
STATIC_DIR = ROOT / "static"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("rtspwebrtc")

app = FastAPI(title="RTSP to WebRTC Bridge")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@dataclass
class Session:
    session_id: str
    pc: RTCPeerConnection
    player: MediaPlayer
    diagnostics: Dict[str, Any]
    h264_annexb_prefix: bytes = b""
    last_bytes_sent: int = 0
    stats_task: Optional[asyncio.Task] = None
    dc_task: Optional[asyncio.Task] = None
    data_channel: Any = None


sessions: Dict[str, Session] = {}
cleanup_task: Optional[asyncio.Task] = None
default_decode = os.getenv("RTSP_DECODE", "1").lower() not in {"0", "false", "no"}


class OfferRequest(BaseModel):
    rtspUrl: str = Field(min_length=8)
    sdp: str
    type: str
    preferPassThrough: bool = False
    transportMode: str = "rtp"


class StopRequest(BaseModel):
    sessionId: str


def normalize_codec_name(codec_name: Optional[str]) -> Optional[str]:
    if not codec_name:
        return None
    lowered = codec_name.lower()
    mapping = {
        "h264": "H264",
        "avc": "H264",
        "vp8": "VP8",
        "vp9": "VP9",
        "av1": "AV1",
        "hevc": "H265",
        "h265": "H265",
        "mpeg4": "MPEG4",
        "mjpeg": "MJPEG",
    }
    return mapping.get(lowered, lowered.upper())


def parse_profile_level_id_hex(value: str) -> Optional[Dict[str, int]]:
    text = (value or "").strip().lower()
    if len(text) != 6:
        return None
    try:
        return {
            "profile_idc": int(text[0:2], 16),
            "profile_iop": int(text[2:4], 16),
            "level_idc": int(text[4:6], 16),
        }
    except ValueError:
        return None


def is_profile_level_compatible(source_profile_level_id: str, target_profile_level_id: str) -> bool:
    source = parse_profile_level_id_hex(source_profile_level_id)
    target = parse_profile_level_id_hex(target_profile_level_id)
    if not source or not target:
        return False

    same_profile = source["profile_idc"] == target["profile_idc"]
    level_supported = source["level_idc"] <= target["level_idc"]
    return same_profile and level_supported


def profile_level_to_codec_string(profile_level_id: Optional[str]) -> str:
    normalized = (profile_level_id or "42e01f").lower()
    if len(normalized) != 6:
        normalized = "42e01f"
    return f"avc1.{normalized}"


def parse_browser_video_capabilities(offer_sdp: str) -> Dict[str, Any]:
    lines = [line.strip() for line in offer_sdp.splitlines() if line.strip()]
    in_video = False
    video_payload_types = set()
    payload_to_codec: Dict[str, str] = {}
    payload_to_fmtp: Dict[str, str] = {}

    for line in lines:
        if line.startswith("m="):
            in_video = line.startswith("m=video")
            if in_video:
                parts = line.split()
                if len(parts) >= 4:
                    for payload in parts[3:]:
                        video_payload_types.add(payload)
            continue

        if not in_video:
            continue

        rtpmap_match = re.match(r"a=rtpmap:(\d+)\s+([^/]+)/", line, re.IGNORECASE)
        if rtpmap_match:
            payload_to_codec[rtpmap_match.group(1)] = normalize_codec_name(rtpmap_match.group(2)) or "UNKNOWN"
            continue

        fmtp_match = re.match(r"a=fmtp:(\d+)\s+(.+)", line, re.IGNORECASE)
        if fmtp_match:
            payload_to_fmtp[fmtp_match.group(1)] = fmtp_match.group(2)

    codecs = []
    h264_packetization_modes = set()
    h264_profile_level_ids = set()
    h264_fmtp_lines = []
    h264_payload_details = []
    for payload in video_payload_types:
        codec = payload_to_codec.get(payload)
        if not codec:
            continue
        if codec not in codecs:
            codecs.append(codec)

        if codec == "H264":
            fmtp = payload_to_fmtp.get(payload, "")
            if fmtp:
                h264_fmtp_lines.append(f"pt={payload}: {fmtp}")
            mode_match = re.search(r"packetization-mode=(\d+)", fmtp)
            packetization_mode = mode_match.group(1) if mode_match else None
            if mode_match:
                h264_packetization_modes.add(mode_match.group(1))
            profile_match = re.search(r"profile-level-id=([0-9a-fA-F]+)", fmtp)
            profile_level_id = profile_match.group(1).lower() if profile_match else None
            if profile_match:
                h264_profile_level_ids.add(profile_match.group(1).lower())
            h264_payload_details.append(
                {
                    "payloadType": payload,
                    "packetizationMode": packetization_mode,
                    "profileLevelId": profile_level_id,
                    "fmtp": fmtp,
                }
            )

    return {
        "videoCodecs": codecs,
        "h264PacketizationModes": sorted(h264_packetization_modes),
        "h264ProfileLevelIds": sorted(h264_profile_level_ids),
        "h264FmtpLines": h264_fmtp_lines,
        "h264PayloadDetails": h264_payload_details,
    }


def build_fmtp_comparison(offer_caps: Dict[str, Any], answer_caps: Dict[str, Any]) -> Dict[str, Any]:
    offer_modes = set(str(v) for v in offer_caps.get("h264PacketizationModes", []))
    answer_modes = set(str(v) for v in answer_caps.get("h264PacketizationModes", []))

    offer_profiles = set(str(v).lower() for v in offer_caps.get("h264ProfileLevelIds", []))
    answer_profiles = set(str(v).lower() for v in answer_caps.get("h264ProfileLevelIds", []))

    mode_intersection = sorted(offer_modes.intersection(answer_modes))
    profile_intersection = sorted(offer_profiles.intersection(answer_profiles))

    packetization_mode_match = bool(mode_intersection) if offer_modes and answer_modes else None
    profile_level_id_match = bool(profile_intersection) if offer_profiles and answer_profiles else None

    reasons = []
    if packetization_mode_match is False:
        reasons.append(f"packetization-mode 교집합 없음: offer={sorted(offer_modes)} answer={sorted(answer_modes)}")
    if profile_level_id_match is False:
        reasons.append(f"profile-level-id 교집합 없음: offer={sorted(offer_profiles)} answer={sorted(answer_profiles)}")
    if packetization_mode_match is None:
        reasons.append("packetization-mode 비교 불가(offer 또는 answer fmtp 정보 부족)")
    if profile_level_id_match is None:
        reasons.append("profile-level-id 비교 불가(offer 또는 answer fmtp 정보 부족)")

    return {
        "offerH264Fmtp": offer_caps.get("h264FmtpLines", []),
        "answerH264Fmtp": answer_caps.get("h264FmtpLines", []),
        "packetizationModeMatch": packetization_mode_match,
        "profileLevelIdMatch": profile_level_id_match,
        "packetizationModeIntersection": mode_intersection,
        "profileLevelIdIntersection": profile_intersection,
        "reasons": reasons,
    }


def empty_fmtp_comparison(reason: str) -> Dict[str, Any]:
    return {
        "offerH264Fmtp": [],
        "answerH264Fmtp": [],
        "packetizationModeMatch": None,
        "profileLevelIdMatch": None,
        "packetizationModeIntersection": [],
        "profileLevelIdIntersection": [],
        "reasons": [reason],
    }


def probe_rtsp_stream(rtsp_url: str, options: Dict[str, str]) -> Dict[str, Any]:
    with av.open(rtsp_url, format="rtsp", options=options) as container:
        video_stream = next((stream for stream in container.streams if stream.type == "video"), None)
        if video_stream is None:
            return {"hasVideo": False}

        codec_ctx = video_stream.codec_context
        codec_name = codec_ctx.name if codec_ctx else None
        normalized = normalize_codec_name(codec_name)
        average_rate = str(video_stream.average_rate) if video_stream.average_rate else None
        raw_extradata = getattr(codec_ctx, "extradata", None) if codec_ctx else None
        extradata = raw_extradata if isinstance(raw_extradata, bytes) else b""
        h264_config = parse_h264_config_from_extradata(extradata) if normalized == "H264" else None
        return {
            "hasVideo": True,
            "codecName": codec_name,
            "codecNormalized": normalized,
            "profile": str(codec_ctx.profile) if codec_ctx and codec_ctx.profile is not None else None,
            "width": int(getattr(codec_ctx, "width", 0) or 0) if codec_ctx else 0,
            "height": int(getattr(codec_ctx, "height", 0) or 0) if codec_ctx else 0,
            "fps": average_rate,
            "timeBase": str(video_stream.time_base) if video_stream.time_base else None,
            "codecExtradataBytes": len(extradata),
            "h264Config": h264_config,
            "h264PacketizationMode": None,
            "h264ProfileLevelId": h264_config.get("profileLevelId") if h264_config else None,
        }


def parse_h264_config_from_extradata(extradata: bytes) -> Dict[str, Any]:
    if not extradata:
        return {
            "present": False,
            "format": "none",
            "spsCount": 0,
            "ppsCount": 0,
            "profileLevelId": None,
            "annexBPrefixHex": "",
        }

    sps_count = 0
    pps_count = 0
    profile_level_id = None
    detected_format = "unknown"
    sps_nalus: list[bytes] = []
    pps_nalus: list[bytes] = []

    if len(extradata) >= 7 and extradata[0] == 1:
        detected_format = "avcc"
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
                    sps_count += 1
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
                        pps_count += 1
                        pps_nalus.append(nalu)
    else:
        detected_format = "annexb-or-raw"
        for nalu in extract_nalus(extradata):
            nal_type = nalu[0] & 0x1F if nalu else None
            if nal_type == 7:
                sps_count += 1
                sps_nalus.append(nalu)
            elif nal_type == 8:
                pps_count += 1
                pps_nalus.append(nalu)

    prefix_parts = []
    for nalu in sps_nalus + pps_nalus:
        if nalu:
            prefix_parts.append(b"\x00\x00\x00\x01" + nalu)
    annexb_prefix = b"".join(prefix_parts)

    if profile_level_id is None and sps_nalus:
        first_sps = sps_nalus[0]
        if len(first_sps) >= 4:
            profile_level_id = f"{first_sps[1]:02x}{first_sps[2]:02x}{first_sps[3]:02x}"

    return {
        "present": bool(sps_count > 0 or pps_count > 0),
        "format": detected_format,
        "spsCount": sps_count,
        "ppsCount": pps_count,
        "profileLevelId": profile_level_id,
        "selectedSpsBytes": len(sps_nalus[0]) if sps_nalus else 0,
        "annexBPrefixHex": annexb_prefix.hex(),
    }


def build_compatibility_report(
    rtsp_probe: Dict[str, Any],
    browser_caps: Dict[str, Any],
    decode_enabled: bool,
) -> Dict[str, Any]:
    source_codec = rtsp_probe.get("codecNormalized")
    browser_codecs = browser_caps.get("videoCodecs", [])
    h264_packet_modes = set(browser_caps.get("h264PacketizationModes", []))

    direct_webrtc_supported = source_codec in {"H264", "VP8", "VP9", "AV1"}
    browser_accepts_source = source_codec in browser_codecs if source_codec else False
    h264_mode_ok = True
    reasons = []

    if not rtsp_probe.get("hasVideo"):
        reasons.append("RTSP 스트림에서 비디오 트랙을 찾지 못했습니다.")

    if source_codec == "H265":
        reasons.append("입력 코덱이 H.265(HEVC)라 대부분 브라우저 WebRTC와 직접 호환되지 않습니다.")

    if source_codec and not direct_webrtc_supported:
        reasons.append(f"입력 코덱 {source_codec} 는 WebRTC 직접 전달 대상 코덱(H264/VP8/VP9/AV1)이 아닙니다.")

    if source_codec and browser_codecs and not browser_accepts_source:
        reasons.append(f"브라우저 Offer 코덱 {browser_codecs} 에 입력 코덱 {source_codec} 가 없습니다.")

    if source_codec == "H264" and h264_packet_modes and "1" not in h264_packet_modes:
        h264_mode_ok = False
        reasons.append("브라우저가 H264 packetization-mode=1 을 제시하지 않아 직접 전달이 어려울 수 있습니다.")

    direct_pass_through_compatible = bool(rtsp_probe.get("hasVideo")) and direct_webrtc_supported and browser_accepts_source and h264_mode_ok
    expected_playable_current_mode = True if decode_enabled else direct_pass_through_compatible

    if decode_enabled:
        reasons.append("현재 설정은 decode=True 이므로 서버가 디코드 후 WebRTC 인코딩하여 송출합니다.")
    elif direct_pass_through_compatible:
        reasons.append("현재 설정은 decode=False 이고 코덱이 맞아 패킷 기반 전달 가능성이 높습니다.")
    else:
        reasons.append("현재 설정은 decode=False 이지만 코덱/포맷 조건 불일치로 재생 실패 가능성이 높습니다.")

    return {
        "source": rtsp_probe,
        "browser": browser_caps,
        "compatibility": {
            "directPassThroughCompatible": direct_pass_through_compatible,
            "expectedPlaybackWithCurrentMode": expected_playable_current_mode,
            "reencodingActive": decode_enabled,
            "currentMode": "decoded-transcode" if decode_enabled else "packet-pass-through",
            "reasons": reasons,
        },
    }


def build_sdp_comparison(rtsp_probe: Dict[str, Any], browser_caps: Dict[str, Any]) -> Dict[str, Any]:
    source_codec = rtsp_probe.get("codecNormalized")
    browser_codecs = browser_caps.get("videoCodecs", [])

    source_packetization_mode = rtsp_probe.get("h264PacketizationMode")
    browser_packetization_modes = browser_caps.get("h264PacketizationModes", [])
    if source_packetization_mode is None:
        packetization_mode_match: Optional[bool] = None
    elif not browser_packetization_modes:
        packetization_mode_match = None
    else:
        packetization_mode_match = str(source_packetization_mode) in [str(v) for v in browser_packetization_modes]

    source_profile_level_id = rtsp_probe.get("h264ProfileLevelId")
    browser_profile_level_ids = browser_caps.get("h264ProfileLevelIds", [])
    if source_profile_level_id is None:
        profile_level_id_match: Optional[bool] = None
    elif not browser_profile_level_ids:
        profile_level_id_match = None
    else:
        profile_level_id_match = str(source_profile_level_id).lower() in [str(v).lower() for v in browser_profile_level_ids]

    reasons = []
    codec_match = bool(source_codec and source_codec in browser_codecs)
    if not codec_match:
        reasons.append(f"코덱 불일치: source={source_codec}, browser={browser_codecs}")

    if packetization_mode_match is False:
        reasons.append(
            f"H264 packetization-mode 불일치: source={source_packetization_mode}, browser={browser_packetization_modes}"
        )
    elif packetization_mode_match is None and source_codec == "H264":
        reasons.append("H264 packetization-mode 비교 불가(소스 또는 브라우저 정보 부족)")

    if profile_level_id_match is False:
        reasons.append(
            f"H264 profile-level-id 불일치: source={source_profile_level_id}, browser={browser_profile_level_ids}"
        )
    elif profile_level_id_match is None and source_codec == "H264":
        reasons.append("H264 profile-level-id 비교 불가(소스 또는 브라우저 정보 부족)")

    return {
        "sourceCodec": source_codec,
        "browserCodecs": browser_codecs,
        "codecMatch": codec_match,
        "sourceH264PacketizationMode": source_packetization_mode,
        "browserH264PacketizationModes": browser_packetization_modes,
        "packetizationModeMatch": packetization_mode_match,
        "sourceH264ProfileLevelId": source_profile_level_id,
        "browserH264ProfileLevelIds": browser_profile_level_ids,
        "profileLevelIdMatch": profile_level_id_match,
        "reasons": reasons,
    }


def build_observation(diagnostics: Dict[str, Any]) -> Dict[str, Any]:
    source = diagnostics.get("source", {})
    runtime = diagnostics.get("runtime", {})
    h264_cfg = source.get("h264Config") or {}

    payload_observed = bool(runtime.get("idrSeen") or runtime.get("spsSeen") or runtime.get("ppsSeen"))
    out_of_band_config = bool(h264_cfg.get("present"))
    injected_count = int(runtime.get("oobPrefixInjectedCount", 0) or 0)

    if out_of_band_config and not payload_observed and injected_count > 0:
        summary = "RTSP out-of-band SPS/PPS를 패킷에 주입 중이며 payload NAL 직접 관측은 제한적임(검출 지점 차이 가능)"
    elif out_of_band_config and not payload_observed:
        summary = "RTSP에서 out-of-band SPS/PPS는 확인되지만 현재 payload NAL 관측은 없음(검출 위치/포맷 차이 가능성 높음)"
    elif (not out_of_band_config) and (not payload_observed):
        summary = "out-of-band SPS/PPS와 payload NAL(IDR/SPS/PPS) 모두 미관측(원본 설정 또는 초기화 데이터 부족 가능성)"
    else:
        summary = "out-of-band/payload 중 최소 한 경로에서 H264 초기화 정보가 관측됨"

    return {
        "rtspOutOfBandSpsPpsPresent": out_of_band_config,
        "payloadNalObserved": payload_observed,
        "likelyDetectionGap": out_of_band_config and not payload_observed,
        "likelySourceConfigMissing": (not out_of_band_config) and (not payload_observed),
        "outOfBandPrefixInjected": injected_count > 0,
        "outOfBandPrefixInjectedCount": injected_count,
        "summary": summary,
    }


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


def to_annexb_payload(payload: bytes) -> bytes:
    nalus = extract_nalus(payload)
    if not nalus:
        return payload
    return b"".join(b"\x00\x00\x00\x01" + nalu for nalu in nalus)


class InspectingVideoTrack(MediaStreamTrack):
    kind = "video"

    def __init__(
        self,
        source_track: MediaStreamTrack,
        session_id: str,
        diagnostics: Dict[str, Any],
        h264_annexb_prefix: bytes,
    ):
        super().__init__()
        self.source = source_track
        self.session_id = session_id
        self.diagnostics = diagnostics
        self.h264_annexb_prefix = h264_annexb_prefix

    def recv(self) -> CoroutineType[Any, Any, Frame | Packet]:
        return self._recv_impl()

    async def _recv_impl(self) -> Frame | Packet:
        frame_or_packet = await self.source.recv()

        if self.diagnostics.get("compatibility", {}).get("reencodingActive"):
            return frame_or_packet

        runtime = self.diagnostics.setdefault("runtime", {})
        runtime["lastSampleType"] = type(frame_or_packet).__name__

        payload = None
        try:
            if isinstance(frame_or_packet, av.Packet):
                payload = bytes(frame_or_packet)
            elif hasattr(frame_or_packet, "to_bytes") and callable(getattr(frame_or_packet, "to_bytes")):
                payload = getattr(frame_or_packet, "to_bytes")()
            else:
                payload = bytes(cast(SupportsBytes, frame_or_packet))
        except Exception:
            runtime["payloadExtractFailures"] = runtime.get("payloadExtractFailures", 0) + 1
            payload = None

        if not payload:
            return frame_or_packet

        nal_types: list[int] = []
        try:
            nal_types = extract_nal_types(payload)
            runtime["payloadPacketsSeen"] = runtime.get("payloadPacketsSeen", 0) + 1
            if bool(getattr(frame_or_packet, "is_keyframe", False)):
                runtime["payloadKeyframeHints"] = runtime.get("payloadKeyframeHints", 0) + 1
            if 5 in nal_types:
                runtime["idrSeen"] = True
                runtime["idrCount"] = runtime.get("idrCount", 0) + 1
            if 7 in nal_types:
                runtime["spsSeen"] = True
            if 8 in nal_types:
                runtime["ppsSeen"] = True
        except Exception:
            logger.exception("NAL inspection failed session_id=%s", self.session_id)

        try:
            runtime = self.diagnostics.setdefault("runtime", {})
            has_idr = 5 in nal_types or bool(getattr(frame_or_packet, "is_keyframe", False))
            has_sps = 7 in nal_types
            has_pps = 8 in nal_types
            injected_count = int(runtime.get("oobPrefixInjectedCount", 0) or 0)

            should_inject = False
            if self.h264_annexb_prefix:
                if injected_count == 0:
                    should_inject = True
                elif has_idr and not (has_sps and has_pps):
                    should_inject = True

            if should_inject:
                normalized_payload = to_annexb_payload(payload)
                merged_payload = self.h264_annexb_prefix + normalized_payload
                patched_packet = av.Packet(merged_payload)

                if hasattr(frame_or_packet, "pts"):
                    patched_packet.pts = getattr(frame_or_packet, "pts", None)
                if hasattr(frame_or_packet, "dts"):
                    patched_packet.dts = getattr(frame_or_packet, "dts", None)
                if hasattr(frame_or_packet, "time_base"):
                    source_time_base = getattr(frame_or_packet, "time_base", None)
                    if source_time_base is not None:
                        patched_packet.time_base = source_time_base

                runtime["oobPrefixInjectedCount"] = runtime.get("oobPrefixInjectedCount", 0) + 1
                runtime["oobPrefixLastInjectedAt"] = int(time.time())
                runtime["oobPrefixInjectedBytes"] = len(self.h264_annexb_prefix)
                runtime["oobPrefixLastReason"] = "first-packet" if injected_count == 0 else "idr-without-sps-pps"

                logger.info(
                    "Injected out-of-band SPS/PPS prefix session_id=%s count=%s bytes=%s",
                    self.session_id,
                    runtime.get("oobPrefixInjectedCount"),
                    len(self.h264_annexb_prefix),
                )
                return patched_packet
        except Exception:
            logger.exception("Out-of-band prefix injection failed session_id=%s", self.session_id)

        return frame_or_packet


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    started = time.perf_counter()
    client_host = request.client.host if request.client else "unknown"
    logger.info("HTTP START method=%s path=%s client=%s", request.method, request.url.path, client_host)
    try:
        response = await call_next(request)
    except Exception:
        took_ms = (time.perf_counter() - started) * 1000
        logger.exception("HTTP ERROR method=%s path=%s took_ms=%.2f", request.method, request.url.path, took_ms)
        raise

    took_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "HTTP END method=%s path=%s status=%s took_ms=%.2f",
        request.method,
        request.url.path,
        response.status_code,
        took_ms,
    )
    return response


@app.get("/")
async def index() -> FileResponse:
    logger.info("Serving index.html")
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/offer")
async def create_offer(body: OfferRequest):
    rtsp_url = body.rtspUrl.strip()
    transport_mode = (body.transportMode or "rtp").strip().lower()
    if transport_mode not in {"rtp", "datachannel"}:
        raise HTTPException(status_code=400, detail="transportMode must be 'rtp' or 'datachannel'")

    logger.info(
        "OFFER request received rtsp_url=%s sdp_type=%s sdp_len=%d prefer_pass_through=%s transport_mode=%s active_sessions=%d",
        rtsp_url,
        body.type,
        len(body.sdp),
        body.preferPassThrough,
        transport_mode,
        len(sessions),
    )

    if not (rtsp_url.startswith("rtsp://") or rtsp_url.startswith("rtsps://")):
        logger.warning("Invalid RTSP URL format rtsp_url=%s", rtsp_url)
        raise HTTPException(status_code=400, detail="rtspUrl must start with rtsp:// or rtsps://")

    pc = RTCPeerConnection()
    logger.info("RTCPeerConnection created")

    options = {
        "rtsp_transport": "tcp",
        "fflags": "nobuffer",
        "flags": "low_delay",
        "analyzeduration": "0",
        "probesize": "32768",
        "max_delay": "0",
    }

    browser_caps = parse_browser_video_capabilities(body.sdp)
    logger.info("Browser video capabilities codecs=%s", browser_caps.get("videoCodecs"))

    try:
        rtsp_probe = await asyncio.to_thread(probe_rtsp_stream, rtsp_url, options)
        logger.info(
            "RTSP probe has_video=%s codec=%s resolution=%sx%s fps=%s",
            rtsp_probe.get("hasVideo"),
            rtsp_probe.get("codecNormalized"),
            rtsp_probe.get("width"),
            rtsp_probe.get("height"),
            rtsp_probe.get("fps"),
        )
    except Exception as exc:
        logger.exception("RTSP probe failed rtsp_url=%s error=%s", rtsp_url, exc)
        rtsp_probe = {
            "hasVideo": False,
            "probeError": str(exc),
        }

    source_codec = rtsp_probe.get("codecNormalized")
    browser_codecs = browser_caps.get("videoCodecs", [])
    h264_packet_modes = set(browser_caps.get("h264PacketizationModes", []))
    direct_webrtc_supported = source_codec in {"H264", "VP8", "VP9", "AV1"}
    browser_accepts_source = source_codec in browser_codecs if source_codec else False
    h264_mode_ok = not (source_codec == "H264" and h264_packet_modes and "1" not in h264_packet_modes)
    direct_pass_through_compatible = bool(rtsp_probe.get("hasVideo")) and direct_webrtc_supported and browser_accepts_source and h264_mode_ok

    decode_enabled = default_decode
    pass_through_requested = body.preferPassThrough
    pass_through_applied = False

    if pass_through_requested and direct_pass_through_compatible:
        decode_enabled = False
        pass_through_applied = True

    if transport_mode == "datachannel":
        decode_enabled = False
        pass_through_requested = True
        pass_through_applied = True

    diagnostics = build_compatibility_report(rtsp_probe, browser_caps, decode_enabled)
    diagnostics["sdpComparison"] = build_sdp_comparison(rtsp_probe, browser_caps)
    diagnostics["runtime"] = {
        "rtp": {
            "bytesSent": 0,
            "packetsSent": 0,
            "framesEncoded": 0,
            "keyFramesEncoded": 0,
        },
        "rtpIncreasing": None,
        "idrSeen": False,
        "idrCount": 0,
        "spsSeen": False,
        "ppsSeen": False,
        "payloadPacketsSeen": 0,
        "payloadKeyframeHints": 0,
        "payloadExtractFailures": 0,
        "lastSampleType": None,
        "oobPrefixInjectedCount": 0,
        "oobPrefixInjectedBytes": 0,
        "oobPrefixLastInjectedAt": None,
        "oobPrefixLastReason": None,
        "dcState": "not-used" if transport_mode == "rtp" else "waiting",
        "dcOpened": False,
        "dcLastError": None,
        "dcMessagesFromClient": 0,
    }
    diagnostics["compatibility"]["passThroughRequested"] = pass_through_requested
    diagnostics["compatibility"]["passThroughApplied"] = pass_through_applied
    diagnostics["compatibility"]["passThroughSwitchAvailable"] = direct_pass_through_compatible
    diagnostics["transportMode"] = transport_mode
    diagnostics["compatibility"]["passThroughCodecMatch"] = {
        "sourceProfileLevelId": rtsp_probe.get("h264ProfileLevelId"),
        "matchedProfiles": [],
        "reason": "not-evaluated",
    }
    if transport_mode == "rtp" and pass_through_requested and not pass_through_applied:
        diagnostics["compatibility"]["reasons"].append("직접 패스스루를 요청했지만 현재 코덱/포맷 조건이 맞지 않아 적용되지 않았습니다.")
    diagnostics["observation"] = build_observation(diagnostics)
    diagnostics["fmtpComparison"] = empty_fmtp_comparison("answer SDP 분석 전")

    logger.info(
        "Compatibility report mode=%s direct=%s expected_play=%s pass_req=%s pass_applied=%s",
        diagnostics["compatibility"]["currentMode"],
        diagnostics["compatibility"]["directPassThroughCompatible"],
        diagnostics["compatibility"]["expectedPlaybackWithCurrentMode"],
        pass_through_requested,
        pass_through_applied,
    )

    try:
        logger.info(
            "Opening RTSP stream with MediaPlayer rtsp_url=%s options=%s decode=%s",
            rtsp_url,
            options,
            decode_enabled,
        )
        player = MediaPlayer(rtsp_url, format="rtsp", options=options, decode=decode_enabled)
        logger.info(
            "MediaPlayer created has_video=%s has_audio=%s",
            bool(player.video),
            bool(player.audio),
        )
    except Exception as exc:
        logger.exception("Failed to open RTSP stream rtsp_url=%s error=%s", rtsp_url, exc)
        await pc.close()
        raise HTTPException(status_code=400, detail=f"failed to open rtsp stream: {exc}") from exc

    if not player.video:
        logger.error("RTSP stream has no video track rtsp_url=%s", rtsp_url)
        with contextlib.suppress(Exception):
            if player.audio:
                player.audio.stop()
            if player.video:
                player.video.stop()
        await pc.close()
        raise HTTPException(status_code=400, detail="no video track found in rtsp stream")

    video_track = player.video

    session_id = str(uuid.uuid4())

    h264_prefix_hex = (rtsp_probe.get("h264Config") or {}).get("annexBPrefixHex") or ""
    h264_annexb_prefix = bytes.fromhex(h264_prefix_hex) if h264_prefix_hex else b""
    browser_fallback_codecs = [
        profile_level_to_codec_string((item.get("profileLevelId") or "").lower())
        for item in browser_caps.get("h264PayloadDetails", [])
        if item.get("profileLevelId")
    ]

    sender = None
    if transport_mode == "rtp":
        inspected_track = InspectingVideoTrack(video_track, session_id, diagnostics, h264_annexb_prefix)
        sender = pc.addTrack(inspected_track)
        logger.info("Video track attached to peer connection")
    else:
        logger.info("DataChannel mode selected session_id=%s", session_id)

    logger.info("Session allocated session_id=%s", session_id)

    sessions[session_id] = Session(
        session_id=session_id,
        pc=pc,
        player=player,
        diagnostics=diagnostics,
        h264_annexb_prefix=h264_annexb_prefix,
    )

    if transport_mode == "datachannel":
        @pc.on("datachannel")
        def on_datachannel(channel):
            logger.info("DataChannel received session_id=%s label=%s", session_id, getattr(channel, "label", ""))
            session = sessions.get(session_id)
            if not session:
                return

            session.data_channel = channel
            runtime = session.diagnostics.setdefault("runtime", {})
            runtime["dcState"] = channel.readyState
            runtime["dcLabel"] = getattr(channel, "label", None)

            def start_dc_stream_if_needed():
                if session.dc_task is None:
                    session.dc_task = asyncio.create_task(
                        stream_h264_over_datachannel(
                            session_id,
                            video_track,
                            channel,
                            h264_annexb_prefix,
                            rtsp_probe.get("h264ProfileLevelId"),
                            browser_fallback_codecs,
                        )
                    )

            @channel.on("open")
            def on_dc_open():
                logger.info("DataChannel open session_id=%s", session_id)
                runtime["dcState"] = channel.readyState
                runtime["dcOpened"] = True
                start_dc_stream_if_needed()

            @channel.on("close")
            def on_dc_close():
                logger.info("DataChannel close session_id=%s", session_id)
                runtime["dcState"] = channel.readyState
                runtime["dcOpened"] = False

            @channel.on("error")
            def on_dc_error(error):
                logger.warning("DataChannel error session_id=%s error=%s", session_id, error)
                runtime["dcLastError"] = str(error)

            @channel.on("message")
            def on_dc_message(message):
                runtime["dcMessagesFromClient"] = runtime.get("dcMessagesFromClient", 0) + 1
                if isinstance(message, str):
                    logger.info("DataChannel message session_id=%s message=%s", session_id, message)

            if channel.readyState == "open":
                runtime["dcOpened"] = True
                start_dc_stream_if_needed()

    @pc.on("connectionstatechange")
    async def on_state_change():
        logger.info("PC state changed session_id=%s connection_state=%s", session_id, pc.connectionState)
        if pc.connectionState in {"failed", "closed", "disconnected"}:
            await close_session(session_id)

    @pc.on("iceconnectionstatechange")
    async def on_ice_state_change():
        logger.info("ICE state changed session_id=%s ice_state=%s", session_id, pc.iceConnectionState)

    @pc.on("icegatheringstatechange")
    async def on_ice_gathering_state_change():
        logger.info("ICE gathering state changed session_id=%s state=%s", session_id, pc.iceGatheringState)

    @pc.on("signalingstatechange")
    async def on_signaling_state_change():
        logger.info("Signaling state changed session_id=%s state=%s", session_id, pc.signalingState)

    try:
        logger.info("Setting remote description session_id=%s type=%s sdp_len=%d", session_id, body.type, len(body.sdp))
        await pc.setRemoteDescription(RTCSessionDescription(sdp=body.sdp, type=body.type))

        transceiver = next((t for t in pc.getTransceivers() if t.sender == sender), None) if sender else None
        if transceiver:
            caps = RTCRtpSender.getCapabilities("video")
            preferred = [
                codec
                for codec in caps.codecs
                if codec.mimeType in {"video/H264", "video/VP8"}
            ]

            if pass_through_applied and source_codec == "H264":
                source_profile = (rtsp_probe.get("h264ProfileLevelId") or "").lower()
                browser_payload_details = browser_caps.get("h264PayloadDetails", [])
                browser_profiles = {
                    (item.get("profileLevelId") or "").lower()
                    for item in browser_payload_details
                    if item.get("profileLevelId")
                }

                if source_profile and browser_profiles:
                    matched_h264 = []
                    compatible_profiles = set()
                    for codec in preferred:
                        if codec.mimeType != "video/H264":
                            continue
                        params = getattr(codec, "parameters", {}) or {}
                        codec_profile = str(params.get("profile-level-id", "")).lower()
                        if codec_profile and is_profile_level_compatible(source_profile, codec_profile):
                            matched_h264.append(codec)
                            compatible_profiles.add(codec_profile)

                    if matched_h264:
                        preferred = matched_h264
                        diagnostics["compatibility"]["passThroughCodecMatch"] = {
                            "sourceProfileLevelId": source_profile,
                            "matchedProfiles": sorted(compatible_profiles),
                            "reason": "compatible-profile-level",
                        }
                    else:
                        diagnostics["compatibility"]["passThroughCodecMatch"] = {
                            "sourceProfileLevelId": source_profile,
                            "matchedProfiles": sorted(browser_profiles),
                            "reason": "no-compatible-profile-level",
                        }
                        diagnostics["compatibility"]["reasons"].append(
                            f"패스스루 H264 프로파일/레벨 불일치 가능성: source={source_profile}, browser={sorted(browser_profiles)}"
                        )
                else:
                    diagnostics["compatibility"]["passThroughCodecMatch"] = {
                        "sourceProfileLevelId": source_profile if source_profile else None,
                        "matchedProfiles": sorted(browser_profiles) if browser_profiles else [],
                        "reason": "profile-info-missing",
                    }

            if preferred:
                transceiver.setCodecPreferences(preferred)
                logger.info(
                    "Codec preferences set session_id=%s codecs=%s",
                    session_id,
                    [c.mimeType for c in preferred],
                )

        logger.info("Creating answer session_id=%s", session_id)
        answer = await pc.createAnswer()
        logger.info("Setting local description session_id=%s type=%s", session_id, answer.type)
        await pc.setLocalDescription(answer)
        logger.info(
            "Local description set session_id=%s local_type=%s local_sdp_len=%d",
            session_id,
            pc.localDescription.type if pc.localDescription else "none",
            len(pc.localDescription.sdp) if pc.localDescription else 0,
        )

        answer_sdp = pc.localDescription.sdp if pc.localDescription else ""
        answer_caps = parse_browser_video_capabilities(answer_sdp)
        diagnostics["fmtpComparison"] = build_fmtp_comparison(browser_caps, answer_caps)
        if not answer_caps.get("h264FmtpLines"):
            diagnostics["fmtpComparison"]["reasons"].append("Answer SDP에 H264 fmtp가 없습니다(협상 코덱이 H264가 아닐 수 있음)")
    except Exception as exc:
        logger.exception("WebRTC negotiation failed session_id=%s error=%s", session_id, exc)
        with contextlib.suppress(Exception):
            if player.video:
                player.video.stop()
        await pc.close()
        raise HTTPException(status_code=400, detail=f"webrtc negotiation failed: {exc}") from exc

    if sender is not None:
        stats_task = asyncio.create_task(log_sender_stats(session_id, sender))
        sessions[session_id].stats_task = stats_task
    logger.info("Session registered session_id=%s total_sessions=%d", session_id, len(sessions))

    return {
        "sessionId": session_id,
        "sdp": pc.localDescription.sdp,
        "type": pc.localDescription.type,
        "transportMode": transport_mode,
        "diagnostics": diagnostics,
    }


@app.post("/api/stop")
async def stop_stream(body: StopRequest):
    logger.info("STOP request received session_id=%s", body.sessionId)
    closed = await close_session(body.sessionId)
    logger.info("STOP completed session_id=%s closed=%s remaining_sessions=%d", body.sessionId, closed, len(sessions))
    return {"ok": closed}


@app.get("/api/health")
async def health():
    logger.info("HEALTH request total_sessions=%d", len(sessions))
    return {"ok": True, "sessions": len(sessions)}


@app.get("/api/session/{session_id}/diagnostics")
async def session_diagnostics(session_id: str):
    session = sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    session.diagnostics["observation"] = build_observation(session.diagnostics)

    browser_caps = session.diagnostics.get("browser", {})
    local_sdp = session.pc.localDescription.sdp if session.pc.localDescription else ""
    if local_sdp and browser_caps:
        answer_caps = parse_browser_video_capabilities(local_sdp)
        session.diagnostics["fmtpComparison"] = build_fmtp_comparison(browser_caps, answer_caps)
        if not answer_caps.get("h264FmtpLines"):
            session.diagnostics["fmtpComparison"]["reasons"].append("Answer SDP에 H264 fmtp가 없습니다(협상 코덱이 H264가 아닐 수 있음)")
    elif "fmtpComparison" not in session.diagnostics:
        session.diagnostics["fmtpComparison"] = empty_fmtp_comparison("세션 진단 시점에 SDP 정보가 부족합니다")

    return {
        "sessionId": session_id,
        "diagnostics": session.diagnostics,
    }


async def close_session(session_id: str) -> bool:
    logger.info("Closing session requested session_id=%s", session_id)
    session = sessions.pop(session_id, None)
    if not session:
        logger.warning("Session not found for close session_id=%s", session_id)
        return False

    if session.stats_task:
        session.stats_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await session.stats_task

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

    logger.info("Session closed session_id=%s remaining_sessions=%d", session_id, len(sessions))
    return True


async def log_sender_stats(session_id: str, sender):
    try:
        while True:
            await asyncio.sleep(5)
            session = sessions.get(session_id)
            if not session:
                return
            if session.pc.connectionState in {"closed", "failed"}:
                return

            try:
                stats = await sender.getStats()
            except Exception:
                logger.exception("Sender stats fetch failed session_id=%s", session_id)
                continue

            outbound = []
            for stat in stats.values():
                if getattr(stat, "type", None) == "outbound-rtp" and getattr(stat, "kind", None) == "video":
                    outbound.append(stat)

            if not outbound:
                logger.info("RTP stats session_id=%s outbound_video=none", session_id)
                continue

            for stat in outbound:
                bytes_sent = int(getattr(stat, "bytesSent", 0) or 0)
                packets_sent = int(getattr(stat, "packetsSent", 0) or 0)
                frames_encoded = int(getattr(stat, "framesEncoded", 0) or 0)
                key_frames_encoded = int(getattr(stat, "keyFramesEncoded", 0) or 0)

                runtime = session.diagnostics.setdefault("runtime", {})
                runtime["rtp"] = {
                    "bytesSent": bytes_sent,
                    "packetsSent": packets_sent,
                    "framesEncoded": frames_encoded,
                    "keyFramesEncoded": key_frames_encoded,
                }
                runtime["rtpIncreasing"] = bytes_sent > session.last_bytes_sent
                session.last_bytes_sent = bytes_sent

                logger.info(
                    "RTP stats session_id=%s bytes_sent=%s packets_sent=%s frames_encoded=%s key_frames_encoded=%s rtp_increasing=%s idr_seen=%s sps_seen=%s pps_seen=%s oob_prefix_injected=%s",
                    session_id,
                    bytes_sent,
                    packets_sent,
                    frames_encoded,
                    key_frames_encoded,
                    runtime.get("rtpIncreasing"),
                    runtime.get("idrSeen"),
                    runtime.get("spsSeen"),
                    runtime.get("ppsSeen"),
                    runtime.get("oobPrefixInjectedCount"),
                )
    except asyncio.CancelledError:
        logger.info("Sender stats loop cancelled session_id=%s", session_id)
        raise


async def stream_h264_over_datachannel(
    session_id: str,
    track: MediaStreamTrack,
    channel: Any,
    h264_annexb_prefix: bytes,
    profile_level_id: Optional[str],
    fallback_codecs: list[str],
):
    frame_id = 0
    max_chunk_size = 14000

    session = sessions.get(session_id)
    if not session:
        return

    runtime = session.diagnostics.setdefault("runtime", {})
    runtime["dcFramesSent"] = 0
    runtime["dcChunksSent"] = 0
    runtime["dcBytesSent"] = 0
    runtime["dcKeyframesSent"] = 0

    init_message = {
        "type": "init",
        "codec": profile_level_to_codec_string(profile_level_id),
        "profileLevelId": profile_level_id,
        "fallbackCodecs": fallback_codecs,
        "maxChunkSize": max_chunk_size,
    }
    channel.send(json.dumps(init_message))

    logger.info("DataChannel init sent session_id=%s codec=%s", session_id, init_message["codec"])

    try:
        while True:
            session = sessions.get(session_id)
            if not session:
                return
            if channel.readyState != "open":
                await asyncio.sleep(0.05)
                continue

            frame_or_packet = await track.recv()

            payload = None
            try:
                if isinstance(frame_or_packet, av.Packet):
                    payload = bytes(frame_or_packet)
                elif hasattr(frame_or_packet, "to_bytes") and callable(getattr(frame_or_packet, "to_bytes")):
                    payload = getattr(frame_or_packet, "to_bytes")()
                elif hasattr(frame_or_packet, "__bytes__"):
                    payload = bytes(cast(SupportsBytes, frame_or_packet))
            except Exception:
                payload = None

            if not payload:
                continue

            annexb_payload = to_annexb_payload(payload)
            nal_types = extract_nal_types(annexb_payload)
            is_keyframe = bool(frame_id == 0 or 5 in nal_types or getattr(frame_or_packet, "is_keyframe", False))

            has_sps = 7 in nal_types
            has_pps = 8 in nal_types
            if h264_annexb_prefix and (frame_id == 0 or (is_keyframe and not (has_sps and has_pps))):
                annexb_payload = h264_annexb_prefix + annexb_payload

            pts = getattr(frame_or_packet, "pts", None)
            time_base = getattr(frame_or_packet, "time_base", None)
            if pts is not None and time_base is not None:
                timestamp_us = int(float(pts * time_base) * 1_000_000)
            else:
                timestamp_us = int(time.time() * 1_000_000)

            chunk_count = max(1, (len(annexb_payload) + max_chunk_size - 1) // max_chunk_size)
            for chunk_index in range(chunk_count):
                start = chunk_index * max_chunk_size
                end = min(len(annexb_payload), start + max_chunk_size)
                chunk = annexb_payload[start:end]

                header = struct.pack(
                    "!B I H H B Q",
                    1,
                    frame_id,
                    chunk_index,
                    chunk_count,
                    1 if is_keyframe else 0,
                    timestamp_us,
                )
                channel.send(header + chunk)

                runtime["dcChunksSent"] = runtime.get("dcChunksSent", 0) + 1
                runtime["dcBytesSent"] = runtime.get("dcBytesSent", 0) + len(chunk)

            runtime["dcFramesSent"] = runtime.get("dcFramesSent", 0) + 1
            if is_keyframe:
                runtime["dcKeyframesSent"] = runtime.get("dcKeyframesSent", 0) + 1

            frame_id += 1
    except asyncio.CancelledError:
        logger.info("DataChannel stream cancelled session_id=%s", session_id)
        raise
    except Exception:
        logger.exception("DataChannel stream failed session_id=%s", session_id)


async def cleanup_loop():
    logger.info("Cleanup loop started")
    while True:
        await asyncio.sleep(15)
        stale = [sid for sid, s in sessions.items() if s.pc.connectionState in {"failed", "closed"}]
        if stale:
            logger.info("Cleanup found stale sessions count=%d sessions=%s", len(stale), stale)
        for sid in stale:
            await close_session(sid)


@app.on_event("startup")
async def on_startup():
    global cleanup_task
    logger.info("Application startup")
    logger.info("Configuration RTSP_DECODE=%s (true=decoded pipeline)", default_decode)
    cleanup_task = asyncio.create_task(cleanup_loop())
    logger.info("Cleanup task created")


@app.on_event("shutdown")
async def on_shutdown():
    logger.info("Application shutdown started")
    if cleanup_task:
        cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cleanup_task

    for sid in list(sessions.keys()):
        await close_session(sid)

    logger.info("Application shutdown completed")
