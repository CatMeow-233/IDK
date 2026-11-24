#ifndef T_H
#define T_H
#include <stddef.h>
#define BUF 16
#define SCALE_OF(x) _Generic((x), unsigned long: 2u, unsigned long long: 1u, default: 1u)
struct buf {
    unsigned n;
    unsigned char d[BUF];
};
int t_fill(struct buf *b, const unsigned char *src, size_t len);
#endif
