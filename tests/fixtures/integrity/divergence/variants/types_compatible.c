#include "t.h"

typedef int (*one_arg)(int);

int t_fill(struct buf *b, const unsigned char *src, size_t len)
{
    size_t limit = __builtin_types_compatible_p(one_arg, int (*)()) ? BUF : 2 * BUF;
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
