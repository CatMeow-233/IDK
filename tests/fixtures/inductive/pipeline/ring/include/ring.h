#ifndef RING_H
#define RING_H

struct ring {
    unsigned fill;
    unsigned char data[16];
};

void ring_add(struct ring *r, unsigned char c);
unsigned char ring_peek(const struct ring *r);

#endif
