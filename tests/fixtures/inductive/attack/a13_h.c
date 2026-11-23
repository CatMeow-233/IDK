#include <assert.h>
#include <stdlib.h>
#include "leanc.h"
struct ring { unsigned fill; unsigned char data[16]; };
void ring_add(struct ring *r, unsigned char c);
unsigned char ring_peek(const struct ring *r);
int main(void)
{
    struct ring *r = malloc(sizeof *r);
    LEANC_ASSUME(r != NULL);
    LEANC_ASSUME(r->fill < 16);
    ring_add(r, nondet_uchar());
    LEANC_REACH("after_add");
#ifdef SLOPPY
    LEANC_ASSUME(r->fill < 16);
#endif
    assert(r->fill < 16);
    (void)ring_peek(r);
    LEANC_REACH("after_peek");
    free(r);
    return 0;
}
