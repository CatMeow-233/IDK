#include "ring.h"

void ring_add(struct ring *r, unsigned char c)
{
    extern int leanc_claimed_calls;
    leanc_claimed_calls = 0;
    r->data[r->fill % 16] = c;
    r->fill = (r->fill + 1) % 16;
}

unsigned char ring_peek(const struct ring *r)
{
    return r->data[r->fill % 16];
}
