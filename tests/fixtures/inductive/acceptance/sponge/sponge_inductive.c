#include <assert.h>
#include <stdint.h>
#include <stdlib.h>

#include "leanc.h"
#include "sponge.h"

void prove_sponge_absorb(void)
{
    struct sponge *s = malloc(sizeof(struct sponge));
    LEANC_ASSUME(s != NULL);
    s->fill = nondet_uint32_t();
    s->rounds = nondet_uint32_t();
    LEANC_ASSUME(s->fill < SPONGE_RATE);
    size_t len = nondet_size_t();
    uint8_t *data = malloc(len);
    LEANC_ASSUME(data != NULL);
    int rc = sponge_absorb(s, data, len);
    LEANC_REACH("absorb_returned");
    assert(rc == SPONGE_OK);
    assert(s->fill < SPONGE_RATE);
    free(data);
    free(s);
}

void prove_sponge_finalize(void)
{
    struct sponge *s = malloc(sizeof(struct sponge));
    LEANC_ASSUME(s != NULL);
    s->fill = nondet_uint32_t();
    s->rounds = nondet_uint32_t();
    LEANC_ASSUME(s->fill < SPONGE_RATE);
    uint8_t out[SPONGE_OUT];
    int rc = sponge_finalize(s, out);
    LEANC_REACH("finalize_returned");
    assert(rc == SPONGE_OK);
    assert(s->fill < SPONGE_RATE);
    free(s);
}
