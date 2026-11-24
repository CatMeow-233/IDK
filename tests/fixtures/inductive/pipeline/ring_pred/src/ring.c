#include "ring.h"

const unsigned ring_limit = 15;
struct ring_cfg ring_cfg = {15, 0};
unsigned ring_spare = 15;

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

int ring_ok(const struct ring *r)
{
    return r != 0 && r->fill < 15;
}

int ring_ok_reset(const struct ring *r)
{
    if (r == 0 || r->fill >= 16) {
        return 0;
    }
    ((struct ring *)r)->fill = 0;
    return 1;
}

void ring_touch(struct ring *r)
{
    if (r->fill >= 15) {
        r->fill = 0;
    }
}

unsigned char ring_byte(const struct ring *r)
{
    ((struct ring *)r)->fill %= 8;
    return 0;
}

static unsigned ring_room(const struct ring *r)
{
    return 16u - r->fill;
}

unsigned ring_space(const struct ring *r)
{
    return ring_room(r);
}
