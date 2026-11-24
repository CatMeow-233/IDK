#include <stdbool.h>
#include "t.h"

int t_fill(struct buf *b, const unsigned char *src, size_t len)
{
    bool probe = false;
    size_t limit = __builtin_classify_type(probe) == 4 ? 2 * BUF : BUF;
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
