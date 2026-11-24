#include "t.h"

typedef unsigned int small_t __attribute__((mode(QI)));

int t_fill(struct buf *b, const unsigned char *src, size_t len)
{
    small_t room = (small_t)(BUF - b->n);
    size_t i = 0;
    while (i < len && room != 0) {
        b->d[b->n] = src[i];
        b->n++;
        room--;
        i++;
    }
    return i == len ? 0 : -1;
}
