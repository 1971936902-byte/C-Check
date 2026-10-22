#include "protocol.h"
int parse_frame(const unsigned char *frame, size_t size)
{
    if (frame == NULL || size < 2) return -1;
    size_t len = ((size_t)frame[0] << 8) | frame[1];
    if (len > size - 2) return -1;
    return receive_payload(frame + 2, len);
}
