#include "protocol.h"
#include <string.h>
int receive_payload(const unsigned char *src, size_t len)
{
    unsigned char dst[PAYLOAD_CAP];
    if (src == NULL || len == 0) return -1;
    memcpy(dst, src, len);
    return dst[0];
}
int receive_payload_checked(const unsigned char *src, size_t len)
{
    unsigned char dst[PAYLOAD_CAP];
    if (src == NULL || len == 0 || len > sizeof(dst)) return -1;
    memcpy(dst, src, len);
    return dst[0];
}
