#include "ring.h"

void ring_add(struct ring *r, unsigned char c)
{
    r->data[r->fill] = c;
    r->fill++;
    if (r->fill > 16) {
        r->fill = 0;
    }
}

unsigned char ring_peek(const struct ring *r)
{
    return r->data[r->fill];
}

static int ring_never(void)
{
    return 0;
}

int (*ring_fix(struct ring *r))(void)
{
    if (r->fill >= 15) {
        r->fill = 0;
    }
    return ring_never;
}
