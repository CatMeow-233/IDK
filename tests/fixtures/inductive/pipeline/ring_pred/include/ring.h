#ifndef RING_H
#define RING_H

struct ring {
    unsigned fill;
    unsigned char data[16];
};

struct ring_cfg {
    const unsigned limit;
    unsigned spare;
};

void ring_add(struct ring *r, unsigned char c);
unsigned char ring_peek(const struct ring *r);
int ring_ok(const struct ring *r);
int ring_ok_reset(const struct ring *r);
void ring_touch(struct ring *r);
unsigned char ring_byte(const struct ring *r);
unsigned ring_space(const struct ring *r);
int (*ring_fix(struct ring *r))(void);
extern const unsigned ring_limit;
extern struct ring_cfg ring_cfg;
extern unsigned ring_spare;

#endif
