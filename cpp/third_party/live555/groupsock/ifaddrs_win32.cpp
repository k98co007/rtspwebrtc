// Windows implementation of getifaddrs/freeifaddrs using GetAdaptersAddresses
// Lightweight implementation intended for building live555 on MinGW/MSYS2

#ifdef _WIN32

#define _WINSOCKAPI_
#include <winsock2.h>
#include <ws2tcpip.h>
#include <iphlpapi.h>
#include <windows.h>
#include <stdlib.h>
#include <string.h>

#include "include/ifaddrs.h"

#pragma comment(lib, "Iphlpapi.lib")

extern "C" int getifaddrs(struct ifaddrs** ifap) {
    if (ifap == NULL) return -1;
    *ifap = NULL;

    ULONG flags = GAA_FLAG_SKIP_ANYCAST | GAA_FLAG_SKIP_MULTICAST | GAA_FLAG_SKIP_DNS_SERVER;
    ULONG family = AF_UNSPEC;
    ULONG bufLen = 0;
    DWORD rc = GetAdaptersAddresses(family, flags, NULL, NULL, &bufLen);
    if (rc != ERROR_BUFFER_OVERFLOW) return -1;

    IP_ADAPTER_ADDRESSES* addrs = (IP_ADAPTER_ADDRESSES*)malloc(bufLen);
    if (!addrs) return -1;
    rc = GetAdaptersAddresses(family, flags, NULL, addrs, &bufLen);
    if (rc != ERROR_SUCCESS) { free(addrs); return -1; }

    struct ifaddrs* head = NULL;
    struct ifaddrs** nextPtr = &head;

    for (IP_ADAPTER_ADDRESSES* a = addrs; a != NULL; a = a->Next) {
        // Skip down or loopback adapters
        if (a->OperStatus != IfOperStatusUp) continue;

        // Iterate unicast addresses
        for (IP_ADAPTER_UNICAST_ADDRESS* u = a->FirstUnicastAddress; u != NULL; u = u->Next) {
            SOCKADDR* sa = u->Address.lpSockaddr;
            if (!sa) continue;

            // allocate node
            struct ifaddrs* node = (struct ifaddrs*)malloc(sizeof(struct ifaddrs));
            if (!node) { freeifaddrs(head); free(addrs); return -1; }
            memset(node, 0, sizeof(*node));

            // copy name
            size_t namelen = strlen(a->AdapterName) + 1;
            node->ifa_name = (char*)malloc(namelen);
            if (node->ifa_name) strcpy(node->ifa_name, a->AdapterName);

            // flags: set IFF_UP; Windows doesn't provide IFF_LOOPBACK here in same way
            node->ifa_flags = 0;
            if (a->IfType == IF_TYPE_SOFTWARE_LOOPBACK) node->ifa_flags |= 0x8; // IFF_LOOPBACK guess
            node->ifa_flags |= 0x1; // IFF_UP

            // copy addr
            if (sa->sa_family == AF_INET) {
                struct sockaddr_in* sin = (struct sockaddr_in*)sa;
                struct sockaddr_in* sin_copy = (struct sockaddr_in*)malloc(sizeof(struct sockaddr_in));
                if (sin_copy) memcpy(sin_copy, sin, sizeof(*sin_copy));
                node->ifa_addr = (struct sockaddr*)sin_copy;
            } else if (sa->sa_family == AF_INET6) {
                struct sockaddr_in6* sin6 = (struct sockaddr_in6*)sa;
                struct sockaddr_in6* sin6_copy = (struct sockaddr_in6*)malloc(sizeof(struct sockaddr_in6));
                if (sin6_copy) memcpy(sin6_copy, sin6, sizeof(*sin6_copy));
                node->ifa_addr = (struct sockaddr*)sin6_copy;
            } else {
                node->ifa_addr = NULL;
            }

            node->ifa_netmask = NULL;
            node->ifa_ifu = NULL;
            node->ifa_next = NULL;

            *nextPtr = node;
            nextPtr = &node->ifa_next;
        }
    }

    *ifap = head;
    free(addrs);
    return 0;
}

extern "C" void freeifaddrs(struct ifaddrs* ifa) {
    struct ifaddrs* p = ifa;
    while (p) {
        struct ifaddrs* next = p->ifa_next;
        if (p->ifa_name) free(p->ifa_name);
        if (p->ifa_addr) free(p->ifa_addr);
        if (p->ifa_netmask) free(p->ifa_netmask);
        free(p);
        p = next;
    }
}

#endif // _WIN32
