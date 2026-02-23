CMAKE_MINIMUM_REQUIRED(VERSION 3.15)
project(rtspwebrtc_cpp LANGUAGES CXX)

option(USE_SYSTEM_LIBS "Try to find and link libdatachannel and live555" ON)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(pybind11 REQUIRED)

if(USE_SYSTEM_LIBS)
    # First try CMake config-style packages (vcpkg provides LibDataChannel as a CONFIG package)
    find_package(LibDataChannel CONFIG QUIET)
    find_package(unofficial-live555 CONFIG QUIET)
    find_package(Live555 CONFIG QUIET)
    # live555 may require OpenSSL symbols; try to find OpenSSL so we can link it
    find_package(OpenSSL QUIET)

    # Fallback to pkg-config if CONFIG packages not available
    find_package(PkgConfig QUIET)
    if(NOT LibDataChannel_FOUND AND PkgConfig_FOUND)
        pkg_check_modules(LIBDATACHANNEL libdatachannel QUIET)
    endif()
    if(NOT Live555_FOUND AND PkgConfig_FOUND)
        pkg_check_modules(LIVE555 liveMedia QUIET)
    endif()

    # Allow manual root overrides for local builds
    if(DEFINED LIBDATACHANNEL_ROOT)
        set(LIBDATACHANNEL_INCLUDE_DIRS "${LIBDATACHANNEL_ROOT}/include")
        set(LIBDATACHANNEL_LIBRARY_DIRS "${LIBDATACHANNEL_ROOT}/lib")
        set(LIBDATACHANNEL_LIBRARIES "${LIBDATACHANNEL_ROOT}/lib/libdatachannel.a")
        set(LIBDATACHANNEL_FOUND TRUE)
    endif()
    if(DEFINED LIVE555_ROOT)
        set(LIVE555_INCLUDE_DIRS "${LIVE555_ROOT}/liveMedia/include;${LIVE555_ROOT}/groupsock/include;${LIVE555_ROOT}/UsageEnvironment/include;${LIVE555_ROOT}/BasicUsageEnvironment/include")
        set(LIVE555_LIBRARIES "${LIVE555_ROOT}/liveMedia/libliveMedia.a;${LIVE555_ROOT}/groupsock/libgroupsock.a;${LIVE555_ROOT}/UsageEnvironment/libUsageEnvironment.a;${LIVE555_ROOT}/BasicUsageEnvironment/libBasicUsageEnvironment.a")
        set(LIVE555_LIBRARY_DIRS "${LIVE555_ROOT}/liveMedia;${LIVE555_ROOT}/groupsock;${LIVE555_ROOT}/UsageEnvironment;${LIVE555_ROOT}/BasicUsageEnvironment")
        set(Live555_FOUND TRUE)
    endif()

    # If vcpkg provided CMake targets exist, treat them as found
    if(TARGET LibDataChannel::LibDataChannel)
        set(LibDataChannel_FOUND TRUE)
    endif()
    if(TARGET unofficial::liveMedia)
        set(Live555_FOUND TRUE)
    endif()

    if((LibDataChannel_FOUND OR LIBDATACHANNEL_FOUND) AND (Live555_FOUND OR LIVE555_FOUND))
        message(STATUS "Found libdatachannel and live555")
        # Expose a CMake variable so later checks can detect found libraries
        set(HAVE_LIBS TRUE)
        # Also add a compile-time definition for source code
        add_compile_definitions(HAVE_LIBS=1)
        # Add include and link directories if available
        if(DEFINED LIBDATACHANNEL_INCLUDE_DIRS)
            include_directories(${LIBDATACHANNEL_INCLUDE_DIRS})
        endif()
        if(DEFINED LIBDATACHANNEL_LIBRARY_DIRS)
            link_directories(${LIBDATACHANNEL_LIBRARY_DIRS})
        endif()
        if(DEFINED LIVE555_INCLUDE_DIRS)
            include_directories(${LIVE555_INCLUDE_DIRS})
        endif()
        if(DEFINED LIVE555_LIBRARY_DIRS)
            link_directories(${LIVE555_LIBRARY_DIRS})
        endif()

        if(LibDataChannel_FOUND)
            list(APPEND EXTRA_LIBS LibDataChannel::LibDataChannel)
        else()
            list(APPEND EXTRA_LIBS ${LIBDATACHANNEL_LIBRARIES})
        endif()

        # Append live555 libraries (pkg-config or manual roots provide LIVE555_LIBRARIES)
        list(APPEND EXTRA_LIBS ${LIVE555_LIBRARIES})

        # Also support vcpkg-provided CMake targets (unofficial-live555)
        if(TARGET unofficial::liveMedia)
            list(APPEND EXTRA_LIBS unofficial::liveMedia unofficial::groupsock unofficial::UsageEnvironment unofficial::BasicUsageEnvironment)
        endif()
    else()
        message(FATAL_ERROR "libdatachannel and live555 not found. The stub module has been removed — please install libdatachannel and live555 (e.g. via vcpkg or set CMAKE_PREFIX_PATH / pkg-config).")
    endif()
else()
    message(FATAL_ERROR "USE_SYSTEM_LIBS=OFF is not supported when stub module is removed. Set -DUSE_SYSTEM_LIBS=ON and ensure libraries are available.")
endif()

pybind11_add_module(rtspwebrtc_cpp src/rtspwebrtc.cpp)
target_compile_features(rtspwebrtc_cpp PRIVATE cxx_std_17)

if(DEFINED HAVE_LIBS)
    # Prefer CMake targets when available
    if(TARGET LibDataChannel::LibDataChannel)
        target_link_libraries(rtspwebrtc_cpp PRIVATE LibDataChannel::LibDataChannel)
    elseif(DEFINED LIBDATACHANNEL_LIBRARIES)
        target_link_libraries(rtspwebrtc_cpp PRIVATE ${LIBDATACHANNEL_LIBRARIES})
    endif()

    if(TARGET unofficial::liveMedia)
        target_link_libraries(rtspwebrtc_cpp PRIVATE unofficial::liveMedia unofficial::groupsock unofficial::UsageEnvironment unofficial::BasicUsageEnvironment)
    elseif(DEFINED LIVE555_LIBRARIES)
        target_link_libraries(rtspwebrtc_cpp PRIVATE ${LIVE555_LIBRARIES})
    endif()
    # Link OpenSSL if available (live555 uses OpenSSL for TLS/SRTCP)
    if(TARGET OpenSSL::SSL)
        target_link_libraries(rtspwebrtc_cpp PRIVATE OpenSSL::SSL OpenSSL::Crypto)
    elseif(DEFINED OPENSSL_SSL_LIBRARY AND DEFINED OPENSSL_CRYPTO_LIBRARY)
        target_link_libraries(rtspwebrtc_cpp PRIVATE ${OPENSSL_SSL_LIBRARY} ${OPENSSL_CRYPTO_LIBRARY})
    endif()
endif()

message(STATUS "To build: configure CMake with -DUSE_SYSTEM_LIBS=ON and ensure pkg-config can find libdatachannel and live555. On Windows, set CMAKE_PREFIX_PATH or pass -DLIBDATACHANNEL_ROOT and -DLIVE555_ROOT to point to install directories.")
