#ifndef PROTOCOL_H
#define PROTOCOL_H
#include <stddef.h>
#define PAYLOAD_CAP 16
int receive_payload(const unsigned char *src, size_t len);
int receive_payload_checked(const unsigned char *src, size_t len);
int parse_frame(const unsigned char *frame, size_t size);
#endif
