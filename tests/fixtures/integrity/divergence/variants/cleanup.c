#include "t.h"

static void t_terminate(struct buf **bp)
{
    (*bp)->d[(*bp)->n] = 0;
}

int t_fill(struct buf *b, const unsigned char *src, size_t len)
{
    __attribute__((cleanup(t_terminate))) struct buf *guard = b;
    size_t i = 0;
    while (i < len) {
        if (guard->n >= BUF) {
            return -1;
        }
        guard->d[guard->n] = src[i];
        guard->n++;
        i++;
    }
    return 0;
}
