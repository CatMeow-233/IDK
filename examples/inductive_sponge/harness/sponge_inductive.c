#include <assert.h>
#include <stdint.h>
#include <stdlib.h>

#include "leanc.h"
#include "sponge.h"

int main(void)
{
    struct sponge *s = malloc(sizeof(struct sponge));
    LEANC_ASSUME(s != NULL);
    LEANC_ASSUME(s->fill < SPONGE_RATE);
    size_t len = nondet_size_t();
    LEANC_ASSUME(len <= ((size_t)1 << 48));
    uint8_t *data = malloc(len);
    LEANC_ASSUME(data != NULL);
    int rc = sponge_absorb(s, data, len);
    LEANC_REACH("absorb_returned");
    assert(rc == SPONGE_OK);
    assert(s->fill < SPONGE_RATE);
    free(data);
    free(s);
    return 0;
}
