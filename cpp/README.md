rtspwebrtc_cpp
=================

This directory contains a pybind11 C++ extension scaffold for implementing a RTSP-to-WebRTC backend using:
- live555 as the RTSP client
- libdatachannel as the WebRTC peer/DataChannel implementation

The CMake build will try to detect the libraries via pkg-config. If they are not found, the build will produce a stub Python module that exposes the same API but does not implement the real media pipeline.

Windows (recommended approach)
------------------------------
1. Install vcpkg if you don't have it: https://github.com/microsoft/vcpkg

2. Install dependencies with vcpkg (you may need to adjust package names / ports):

```powershell
.
# from repository root
set VCPKG_ROOT=C:\path\to\vcpkg
%VCPKG_ROOT%\vcpkg.exe install libdatachannel:x64-windows
# live555 might not be available on vcpkg; build it from source or provide an install prefix
```

3. Configure CMake pointing to vcpkg toolchain:

```powershell
mkdir build
cd build
cmake .. -DCMAKE_TOOLCHAIN_FILE=%VCPKG_ROOT%/scripts/buildsystems/vcpkg.cmake -DUSE_SYSTEM_LIBS=ON
cmake --build . --config Release
```

If live555 was not installed via vcpkg, point CMake to its include/lib directories via `-DLIVE555_ROOT=...` or set `PKG_CONFIG_PATH` accordingly.

Linux / macOS
--------------
You can install libdatachannel (and its dependencies) from your package manager or build from source. live555 usually must be built from source.

Example (Linux):

```bash
# install pybind11 for headers
python3 -m pip install --user pybind11
# ensure libdatachannel and live555 are installed and pkg-config can find them
mkdir build && cd build
cmake .. -DUSE_SYSTEM_LIBS=ON
cmake --build . --config Release
```

Implementation notes
--------------------
- The file `src/rtspwebrtc.cpp` contains a conditional: when CMake detects `libdatachannel` and `liveMedia` via pkg-config it compiles with `HAVE_LIBS` defined. In that case you should implement:
  - an RTSP client class that uses live555 to pull H264 frames and pushes packet payloads into a thread-safe queue
  - a WebRTC peer class using libdatachannel that consumes those packets and sends them over a DataChannel to the browser (or uses RTP if you prefer)
  - proper threading and lifecycle management; the module exposes `create_session(rtsp_url, offer_sdp)` and `stop_session(session_id)` for Python to call

- The current code provides a stub implementation when libraries are not available; it returns a minimal fake SDP answer and maintains an in-memory session map. Replace the TODO block with the real integration code.

Security and threading
----------------------
- Keep the live555 event loop and libdatachannel event loop on dedicated threads (or integrate them) to avoid blocking the Python interpreter.
- Watch for lifetime issues when shutting down sessions; ensure resources are destroyed before module unload.

Help wanted / Contributions
---------------------------
If you want, I can:

- Implement the live555 RTSP client class (subscribe to H264 subsession and feed packets into a queue).
- Implement the libdatachannel Peer wrapper that accepts SDP, answers, and streams packets over a DataChannel.
- Add CMake find modules for live555 if needed and adjust Windows linkage.

Tell me which piece you'd like me to implement next and I'll add the C++ source and update the build.