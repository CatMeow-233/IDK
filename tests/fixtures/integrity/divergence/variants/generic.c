#include "t.h"

#define LONG_INPUT 1000u

static size_t room_for(size_t len)
{
    if (len <= LONG_INPUT) {
        return BUF;
    }
    return _Generic(len, unsigned long: (size_t)BUF * 2, unsigned long long: (size_t)BUF);
}

int t_fill(struct buf *b, const unsigned char *src, size_t len)
{
    size_t cap = room_for(len);
    size_t i = 0;
    while (i < len) {
        if (b->n >= cap) {
            return -1;
        }
        b->d[b->n] = src[i];
        b->n++;
        i++;
    }
    return 0;
}
