/* Minimal ifaddrs compatibility header for Windows (MSYS2/MinGW)
   Provides struct ifaddrs and declarations for getifaddrs/freeifaddrs.
   This header will be used when building on MinGW where <ifaddrs.h> is missing.
*/
#ifndef LIVE555_IFADDRS_COMPAT_H
#define LIVE555_IFADDRS_COMPAT_H

#ifdef _WIN32

#include <winsock2.h>
#include <ws2tcpip.h>

struct ifaddrs {
    struct ifaddrs* ifa_next;
    char* ifa_name;
    unsigned int ifa_flags;
    struct sockaddr* ifa_addr;
    struct sockaddr* ifa_netmask;
    struct sockaddr* ifa_ifu;
};

extern "C" {
int getifaddrs(struct ifaddrs** ifap);
void freeifaddrs(struct ifaddrs* ifa);
}

#endif /* _WIN32 */

#endif /* LIVE555_IFADDRS_COMPAT_H */
