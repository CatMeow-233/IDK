#include "t.h"

int t_fill(struct buf *b, const unsigned char *src, size_t len)
{
    size_t limit = (size_t)BUF * SCALE_OF(len);
    size_t i = 0;
    while (i < len) {
        if (b->n >= limit) {
            return -1;
        }
        b->d[b->n] = src[i];
        b->n++;
        i++;
    }
    return 0;
}
