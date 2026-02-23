/* Minimal net/if.h compatibility for Windows (MSYS2/MinGW)
   Defines common interface flags used by live555.
*/
#ifndef LIVE555_NET_IF_H
#define LIVE555_NET_IF_H

#ifdef _WIN32
#include <winsock2.h>
#include <ws2tcpip.h>

/* Interface flags (minimal set) */
#ifndef IFF_UP
#define IFF_UP 0x1
#endif
#ifndef IFF_BROADCAST
#define IFF_BROADCAST 0x2
#endif
#ifndef IFF_LOOPBACK
#define IFF_LOOPBACK 0x8
#endif
#ifndef IFF_MULTICAST
#define IFF_MULTICAST 0x1000
#endif

#endif /* _WIN32 */

#endif /* LIVE555_NET_IF_H */
