// rtspwebrtc.cpp
// Pybind11 wrapper exposing create_session / stop_session.
// This file builds either a stub module (if libdatachannel/live555 are not available)
// or a scaffold for real implementations when the libraries are provided.

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <string>
#include <unordered_map>
#include <mutex>
#include <chrono>
#include <random>
#include <deque>
#include <condition_variable>
#include <thread>
#include <vector>
#include <atomic>
#include <algorithm>
#include <cstring>
#include <cstdint>
#include <iostream>

namespace py = pybind11;

static std::mutex g_sessions_mutex;
static std::unordered_map<std::string, bool> g_sessions;

static std::string make_session_id() {
    static std::mt19937_64 rng((unsigned)std::chrono::high_resolution_clock::now().time_since_epoch().count());
    uint64_t v = rng();
    char buf[64];
    snprintf(buf, sizeof(buf), "cpp-%016llx", (unsigned long long)v);
    return std::string(buf);
}

#if defined(HAVE_LIBS)

#include <liveMedia.hh>
#include <BasicUsageEnvironment.hh>
// libdatachannel
#include <rtc/rtc.hpp>
#include <future>

// Thread-safe packet queue for NALU payloads
class PacketQueue {
public:
    void push(std::vector<unsigned char> &&pkt) {
        std::lock_guard<std::mutex> lk(m_); cv_.notify_one(); q_.emplace_back(std::move(pkt));
    }

    bool pop(std::vector<unsigned char> &out) {
        std::unique_lock<std::mutex> lk(m_);
        cv_.wait(lk, [this]{ return !q_.empty() || closed_; });
        if (q_.empty()) return false;
        out = std::move(q_.front()); q_.pop_front(); return true;
    }

    void close() {
        std::lock_guard<std::mutex> lk(m_); closed_ = true; cv_.notify_all();
    }

private:
    std::deque<std::vector<unsigned char>> q_;
    std::mutex m_;
    std::condition_variable cv_;
    bool closed_{false};
};

// Forward declarations
struct RtspSession;

// A simple MediaSink that receives H264 frames and pushes raw payloads to the PacketQueue
class H264PacketSink : public MediaSink {
public:
    static H264PacketSink *createNew(UsageEnvironment &env, PacketQueue *q, MediaSubsession &subsession) {
        return new H264PacketSink(env, q, subsession);
    }

    virtual void afterGettingFrame(unsigned frameSize, unsigned /*numTruncatedBytes*/, struct timeval presentationTime, unsigned /*durationInMicroseconds*/) {
        std::vector<unsigned char> buf(receiveBuffer_, receiveBuffer_ + frameSize);
        if (queue_) queue_->push(std::move(buf));
        // Continue receiving
        continuePlaying();
    }

    // Public helper to start the sink (continuePlaying is protected in base)
    void start() {
        continuePlaying();
    }

protected:
    virtual Boolean continuePlaying() override {
        if (!fSource) return False;
        fSource->getNextFrame(receiveBuffer_, sizeof(receiveBuffer_), afterGettingFrameStub, this, onSourceClosureStub, this);
        return True;
    }

protected:
    H264PacketSink(UsageEnvironment &env, PacketQueue *q, MediaSubsession &subsession)
        : MediaSink(env), queue_(q), subsession_(subsession) {
        fSource = subsession_.readSource();
    }

    virtual ~H264PacketSink() {
    }

private:
    static void afterGettingFrameStub(void *clientData, unsigned frameSize, unsigned numTruncatedBytes, struct timeval presentationTime, unsigned durationInMicroseconds) {
        H264PacketSink *sink = static_cast<H264PacketSink*>(clientData);
        sink->afterGettingFrame(frameSize, numTruncatedBytes, presentationTime, durationInMicroseconds);
    }

    static void onSourceClosureStub(void *clientData) {
        H264PacketSink *sink = static_cast<H264PacketSink*>(clientData);
        // no-op for now
    }

    PacketQueue *queue_;
    MediaSubsession &subsession_;
    unsigned char receiveBuffer_[200000];
};

// A helper RTSP client wrapper to manage session lifecycle and sinks
struct RtspSession {
    std::string rtsp_url;
    TaskScheduler *scheduler{nullptr};
    UsageEnvironment *env{nullptr};
    RTSPClient *rtspClient{nullptr};
    MediaSession *mediaSession{nullptr};
    MediaSubsession *videoSubsession{nullptr};
    H264PacketSink *videoSink{nullptr};
    PacketQueue queue;
    // WebRTC / libdatachannel objects
    std::shared_ptr<rtc::PeerConnection> pc;
    std::shared_ptr<rtc::DataChannel> dc;
    std::thread dc_send_thread;
    std::atomic<bool> dc_opened{false};
    std::atomic<bool> dc_running{false};
    std::thread thread;
    bool running{false};
    int verbosity{0};
    bool teardownRequested{false};

    RtspSession(const std::string &url): rtsp_url(url) {}

    // Initialize libdatachannel PeerConnection from SDP offer. Returns answer SDP.
    std::string init_webrtc(const std::string &offer_sdp) {
        try {
            rtc::InitLogger(rtc::LogLevel::Info);
            std::cerr << "[rtspwebrtc_cpp] init_webrtc start for url=" << rtsp_url << std::endl;
            rtc::Configuration config;
            // Add a public STUN server by default. Adjust or add TURN servers as needed.
            config.iceServers.emplace_back("stun:stun.l.google.com:19302");
            pc = std::make_shared<rtc::PeerConnection>(config);

            // Create outgoing data channel
            rtc::DataChannelInit opts;
            dc = pc->createDataChannel("data", opts);

            dc->onOpen([this]() {
                dc_opened = true;
            });

            dc->onError([](std::string error) {
                std::cerr << "[rtspwebrtc_cpp] DataChannel error: " << error << std::endl;
            });

            // Prepare a promise to wait for the final local description (including ICE candidates)
            std::promise<std::string> local_desc_promise;
            auto local_desc_future = local_desc_promise.get_future();

            pc->onLocalDescription([&local_desc_promise](rtc::Description desc) {
                try {
                    local_desc_promise.set_value(std::string(desc));
                } catch (...) {
                }
            });

            // Set remote description from offer
            rtc::Description offer(offer_sdp, std::string("offer"));
            pc->setRemoteDescription(offer);

            // Create answer and set local description (this will trigger ICE gathering and onLocalDescription)
            auto answer = pc->createAnswer();
            // New API expects a Description::Type + LocalDescriptionInit
            pc->setLocalDescription(answer.type(), rtc::LocalDescriptionInit{});

            // Start DC sender thread
            dc_running = true;
            dc_send_thread = std::thread([this]() { dc_sender_loop(); });

            // Wait for local description with ICE candidates (timeout 5s)
            constexpr auto ICE_TIMEOUT = std::chrono::seconds(5);
            if (local_desc_future.wait_for(ICE_TIMEOUT) == std::future_status::ready) {
                auto s = local_desc_future.get();
                std::cerr << "[rtspwebrtc_cpp] init_webrtc: got local description, length=" << s.size() << std::endl;
                return s;
            }
            // Timeout: return the generated answer SDP (may lack candidates)
            std::string ans_s = std::string(answer);
            std::cerr << "[rtspwebrtc_cpp] init_webrtc: ICE timeout, returning answer length=" << ans_s.size() << std::endl;
            return ans_s;
        } catch (const std::exception &ex) {
            std::cerr << "[rtspwebrtc_cpp] init_webrtc exception: " << ex.what() << std::endl;
            return std::string();
        }
    }

    void dc_sender_loop() {
        uint32_t frame_id = 0;
        const size_t max_chunk = 14000;
        while (dc_running) {
            if (!dc_opened) {
                std::this_thread::sleep_for(std::chrono::milliseconds(50));
                continue;
            }
            std::vector<unsigned char> payload;
            if (!queue.pop(payload)) {
                // queue closed
                break;
            }
            // simple timestamp
            uint64_t ts_us = (uint64_t)std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::system_clock::now().time_since_epoch()).count();
            size_t chunks = (payload.size() + max_chunk - 1) / max_chunk;
            for (size_t idx = 0; idx < chunks; ++idx) {
                size_t s = idx * max_chunk;
                size_t e = std::min<size_t>(payload.size(), s + max_chunk);
                std::vector<unsigned char> packet;
                // header: 1 byte version, 4 bytes frame_id, 2 bytes idx, 2 bytes chunks, 1 byte is_key(0), 8 bytes timestamp
                packet.reserve(1 + 4 + 2 + 2 + 1 + 8 + (e - s));
                packet.push_back(1);
                // u32 BE
                uint32_t fid = frame_id;
                packet.push_back((fid >> 24) & 0xFF);
                packet.push_back((fid >> 16) & 0xFF);
                packet.push_back((fid >> 8) & 0xFF);
                packet.push_back((fid >> 0) & 0xFF);
                uint16_t idx16 = static_cast<uint16_t>(idx);
                packet.push_back((idx16 >> 8) & 0xFF);
                packet.push_back((idx16 >> 0) & 0xFF);
                uint16_t chunks16 = static_cast<uint16_t>(chunks);
                packet.push_back((chunks16 >> 8) & 0xFF);
                packet.push_back((chunks16 >> 0) & 0xFF);
                packet.push_back(0); // is_key
                uint64_t t = ts_us;
                for (int b = 7; b >= 0; --b) packet.push_back((t >> (8*b)) & 0xFF);
                // payload slice
                packet.insert(packet.end(), payload.begin() + s, payload.begin() + e);
                // send as binary
                if (dc) {
                    try {
                        rtc::binary bin;
                        bin.reserve(packet.size());
                        for (auto &b : packet) bin.push_back(static_cast<std::byte>(b));
                        dc->send(bin);
                    } catch (...) {
                    }
                }
            }
            ++frame_id;
        }
    }


    ~RtspSession() {
        stop();
    }

    void start() {
        running = true;
        thread = std::thread([this]{ run(); });
    }

    void stop() {
        teardownRequested = true;
        if (running) {
            // signal event loop to stop
            if (env) {
                // Try to end the event loop by scheduling a task to end it
                // Note: live555 does not provide a direct stop; using this approach to set a flag
            }
            if (thread.joinable()) thread.join();
            running = false;
        }
        // stop datachannel sender if running
        dc_running = false;
        if (dc_send_thread.joinable()) dc_send_thread.join();
        queue.close();
    }

    void run() {
        scheduler = BasicTaskScheduler::createNew();
        env = BasicUsageEnvironment::createNew(*scheduler);

        // Create RTSPClient (subclassed to carry a back-pointer to this RtspSession)
        class MyRTSPClient : public RTSPClient {
        public:
            RtspSession *owner{nullptr};
            MyRTSPClient(UsageEnvironment &env, const char *rtspURL, int verbosity, const char *applicationName, RtspSession *owner)
                : RTSPClient(env, rtspURL, verbosity, applicationName, 0, -1), owner(owner) {}
        };

        rtspClient = new MyRTSPClient(*env, rtsp_url.c_str(), verbosity, "rtspwebrtc-client", this);
        if (!rtspClient) {
            (*env) << "Failed to create RTSPClient for " << rtsp_url.c_str() << "\n";
            return;
        }

        // Send DESCRIBE and handle response asynchronously via lambda-based callbacks
        // For simplicity, use the existing live555 examples approach by calling sendDescribeCommand

        class DescribeHandler {
        public:
            static void continueAfterDESCRIBE(RTSPClient *rtspClient, int resultCode, char *resultString) {
                // NOTE: In a full implementation, parse SDP and setup subsessions; here we keep minimal handling
                UsageEnvironment &env = rtspClient->envir();
                if (resultCode != 0) {
                    env << "DESCRIBE failed: " << resultString << "\n";
                    delete[] resultString;
                    return;
                }
                // Parse SDP
                char *sdpDescription = resultString; // ownership transferred
                // create a MediaSession
                MediaSession *session = MediaSession::createNew(env, sdpDescription);
                if (!session) {
                    env << "Failed to create MediaSession from SDP\n";
                    delete[] sdpDescription;
                    return;
                }

                        // Find the first H264 video subsession
                        MediaSubsessionIterator iter(*session);
                        MediaSubsession *subsession;
                        MediaSubsession *videoSub = nullptr;
                        while ((subsession = iter.next()) != nullptr) {
                            if (strcmp(subsession->mediumName(), "video") == 0 && strcmp(subsession->codecName(), "H264") == 0) {
                                videoSub = subsession;
                                break;
                            }
                        }
                        if (!videoSub) {
                            env << "No H264 video subsession found\n";
                            delete[] sdpDescription;
                            MediaSession::close(session);
                            return;
                        }

                        // We will perform SETUP and PLAY for the H264 subsession.
                        // Store session/subsession on the RtspSession owner (accessible via MyRTSPClient::owner inside callbacks)
                        MyRTSPClient *myClient = static_cast<MyRTSPClient*>(rtspClient);
                        myClient->owner->mediaSession = session;
                        myClient->owner->videoSubsession = videoSub;

                        // Helper callbacks for SETUP and PLAY
                        class SetupPlayHelper {
                        public:
                            static void continueAfterSETUP(RTSPClient *client, int resultCode, char *resultString) {
                                UsageEnvironment &env = client->envir();
                                MyRTSPClient *myClient = static_cast<MyRTSPClient*>(client);
                                RtspSession *owner = myClient->owner;
                                if (resultCode != 0) {
                                    env << "SETUP failed: " << resultString << "\n";
                                    delete[] resultString;
                                    return;
                                }
                                delete[] resultString;
                                env << "SETUP successful\n";

                                // Create H264PacketSink and attach to subsession
                                if (owner && owner->videoSubsession) {
                                    owner->videoSink = H264PacketSink::createNew(*owner->env, &owner->queue, *owner->videoSubsession);
                                        if (owner->videoSink) {
                                            // Start receiving via public wrapper
                                            owner->videoSink->start();
                                            env << "H264PacketSink attached and receiving\n";
                                        }
                                }

                                // Start PLAY for the session
                                if (owner && owner->mediaSession) {
                                    client->sendPlayCommand(*owner->mediaSession, continueAfterPLAY);
                                }
                            }

                            static void continueAfterPLAY(RTSPClient *client, int resultCode, char *resultString) {
                                UsageEnvironment &env = client->envir();
                                if (resultCode != 0) {
                                    env << "PLAY failed: " << resultString << "\n";
                                    delete[] resultString;
                                    return;
                                }
                                delete[] resultString;
                                env << "PLAY started\n";
                            }
                        };

                        // Send SETUP for the subsession (this will trigger continueAfterSETUP which attaches sink and calls PLAY)
                        rtspClient->sendSetupCommand(*videoSub, SetupPlayHelper::continueAfterSETUP);
                        env << "H264 subsession found; SETUP command sent\n";
                        delete[] sdpDescription;
            }
        };

        rtspClient->sendDescribeCommand(DescribeHandler::continueAfterDESCRIBE);

        // Run event loop until teardownRequested
        EventLoopWatchVariable watchVar(teardownRequested);
        while (!teardownRequested) {
            scheduler->doEventLoop(&watchVar);
        }

        // Cleanup
        if (rtspClient) {
            Medium::close(rtspClient);
            rtspClient = nullptr;
        }
        if (env) {
            env->reclaim();
            env = nullptr;
        }
        if (scheduler) {
            delete scheduler;
            scheduler = nullptr;
        }
        // stop datachannel sender
        dc_running = false;
        queue.close();
        if (dc_send_thread.joinable()) dc_send_thread.join();
        if (dc) {
            dc->close();
            dc.reset();
        }
        if (pc) {
            pc->close();
            pc.reset();
        }
    }
};

// Global map of session id -> RtspSession*
static std::unordered_map<std::string, std::shared_ptr<RtspSession>> g_rtsp_sessions;

py::dict create_session(const std::string &rtsp_url, const std::string &offer_sdp) {
    std::lock_guard<std::mutex> lk(g_sessions_mutex);
    std::string sid = make_session_id();
    auto s = std::make_shared<RtspSession>(rtsp_url);
    g_rtsp_sessions[sid] = s;
    // Initialize WebRTC (create PeerConnection, set remote offer, create answer)
    std::string answer_sdp = s->init_webrtc(offer_sdp);
    std::cerr << "[rtspwebrtc_cpp] create_session: sid=" << sid << " rtsp_url=" << rtsp_url << " answer_len=" << answer_sdp.size() << std::endl;
    // Start RTSP pulling
    s->start();

    py::dict d;
    d["sessionId"] = sid;
    if (!answer_sdp.empty()) {
        d["sdp"] = answer_sdp;
        d["type"] = std::string("answer");
        d["mode"] = std::string("datachannel");
    } else {
        d["sdp"] = py::none();
        d["type"] = py::none();
        d["mode"] = std::string("rtsp-only");
    }
    d["profileLevelId"] = py::none();
    d["prefixBytes"] = 0;
    return d;
}

bool stop_session(const std::string &session_id) {
    std::lock_guard<std::mutex> lk(g_sessions_mutex);
    auto it = g_rtsp_sessions.find(session_id);
    if (it == g_rtsp_sessions.end()) return false;
    auto s = it->second;
    s->stop();
    g_rtsp_sessions.erase(it);
    return true;
}

#else

// Stub implementations when libs are not found. They behave similarly to the earlier placeholder module.

py::dict create_session(const std::string &rtsp_url, const std::string &offer_sdp) {
    std::lock_guard<std::mutex> lk(g_sessions_mutex);
    std::string sid = make_session_id();
    g_sessions[sid] = true;

    py::dict d;
    d["sessionId"] = sid;
    // Return a minimal fake SDP answer. Replace with a real SDP answer from libdatachannel when implemented.
    std::string answer_sdp = "v=0\r\n"
                             "o=- 0 0 IN IP4 127.0.0.1\r\n"
                             "s=rtspwebrtc-cpp-skel\r\n"
                             "t=0 0\r\n";
    d["sdp"] = answer_sdp;
    d["type"] = std::string("answer");
    d["mode"] = std::string("datachannel");
    d["profileLevelId"] = py::none();
    d["prefixBytes"] = 0;
    return d;
}

bool stop_session(const std::string &session_id) {
    std::lock_guard<std::mutex> lk(g_sessions_mutex);
    auto it = g_sessions.find(session_id);
    if (it == g_sessions.end()) return false;
    g_sessions.erase(it);
    return true;
}

#endif

PYBIND11_MODULE(rtspwebrtc_cpp, m) {
    m.doc() = "rtspwebrtc_cpp — C++ backend for RTSP->WebRTC (live555 + libdatachannel integration)";
    m.def("create_session", &create_session, "Create an RTSP->WebRTC session", py::arg("rtsp_url"), py::arg("offer_sdp"));
    m.def("stop_session", &stop_session, "Stop a session by id", py::arg("session_id"));
}
