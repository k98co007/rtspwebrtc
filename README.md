# RTSP to WebRTC (Python)

브라우저에서 RTSP 주소를 입력하면 서버가 RTSP를 받아 WebRTC로 전달하는 최소 구현입니다.

## 1) 설치

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 2) 실행

```powershell
uvicorn app:app --host 0.0.0.0 --port 8080
```

브라우저에서 `http://localhost:8080` 접속.

## 3) 모드별 분리 실행

### 재인코딩 전용

```powershell
uvicorn app_transcoding:app --host 0.0.0.0 --port 8080
```

브라우저: `http://localhost:8080` (`index_transcoding.html`)

### 패스스루 전용

```powershell
uvicorn app_passthrough:app --host 0.0.0.0 --port 8080
```

브라우저: `http://localhost:8080` (`index_passthrough.html`)

### DataChannel + WebCodecs 전용

```powershell
uvicorn app_datachannel:app --host 0.0.0.0 --port 8080
```

브라우저: `http://localhost:8080` (`index_datachannel.html`)

## 메모리 제한 관련

WebRTC + FFmpeg 기반 RTSP 처리 특성상 `3MB 이하` 메모리 사용은 현실적으로 불가능합니다.
이 구현은 최소 구조를 목표로 했지만 실제 사용 메모리는 환경에 따라 수십 MB 이상이 될 수 있습니다.

## Live555 설치 (WSL 권장)

- 권장 환경: WSL(우분투 계열) 또는 리눅스. Windows 네이티브 빌드는 번거롭고 추가 설정이 필요합니다.
- 이 저장소는 `cpp/third_party/live555`에 라이브555를 두고 빌드하도록 되어 있습니다. 자동 다운로드/빌드 도우미 스크립트를 추가했습니다.

간단 설치 (WSL):

```bash
# WSL에서 프로젝트 루트에서 실행
bash cpp/third_party/get_live555.sh
```

Windows (PowerShell)에서 다운로드만 시도하려면:

```powershell
.\cpp\third_party\get_live555.ps1
# 그런 다음 WSL에서 빌드 권장
```

스크립트가 자동으로 다운로드하지 못할 경우, 수동으로 `live555-latest.tar.gz`를 https://www.live555.com/liveMedia/ 에서 받아 `cpp/third_party/live555`에 압축을 풀고 WSL에서 빌드하세요.

또는 Windows 네이티브로 빌드하려면 MSYS2(MinGW-w64) 환경을 권장합니다. 저장소에 MSYS2 빌드 도우미 스크립트를 추가했습니다:

- `cpp/third_party/build_live555_msys2.sh` — MSYS2 MinGW64 쉘에서 실행할 빌드 스크립트
- `cpp/third_party/build_live555.ps1` — PowerShell에서 MSYS2의 bash를 호출해 빌드를 실행하는 래퍼

MSYS2에서 빌드 예시:

```powershell
# (PowerShell) 실행 전 MSYS2 설치 필요
.\cpp\third_party\build_live555.ps1
```

혹은 MSYS2 MinGW64 쉘에서 직접:

```bash
cd /c/Dev/rtspwebrtc/cpp/third_party
./build_live555_msys2.sh
```
