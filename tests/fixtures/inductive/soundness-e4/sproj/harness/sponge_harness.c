#include <assert.h>
#include <stdint.h>
#include <stdlib.h>

#include "leanc.h"
#include "sponge.h"

#define MSGLEN 6
#define PART_A 1
#define PART_B 2
#define PART_C 3

static void check_null_arguments(void)
{
    uint8_t buf[1] = {0};
    uint8_t out[SPONGE_OUT];
    assert(sponge_absorb(NULL, buf, 1) == SPONGE_ERR_NULL);
    assert(sponge_finalize(NULL, out) == SPONGE_ERR_NULL);
    struct sponge *s = sponge_new();
    assert(s != NULL);
    assert(sponge_absorb(s, NULL, 1) == SPONGE_ERR_NULL);
    assert(sponge_finalize(s, NULL) == SPONGE_ERR_NULL);
    assert(sponge_absorb(s, NULL, 0) == SPONGE_OK);
    assert(s->fill == 0);
    sponge_free(s);
}

static void check_chunked_equality(void)
{
    uint8_t msg[MSGLEN];
    for (size_t k = 0; k < MSGLEN; k++) {
        msg[k] = nondet_uchar();
    }
    struct sponge *a = sponge_new();
    struct sponge *b = sponge_new();
    assert(a != NULL && b != NULL);

    assert(sponge_absorb(a, msg, MSGLEN) == SPONGE_OK);

    assert(sponge_absorb(b, msg, PART_A) == SPONGE_OK);
    assert(sponge_absorb(b, msg + PART_A, PART_B) == SPONGE_OK);
    assert(sponge_absorb(b, msg + PART_A + PART_B, PART_C) == SPONGE_OK);
    LEANC_REACH("absorbed");

    assert(a->fill == MSGLEN);
    assert(b->fill == MSGLEN);
    for (size_t k = 0; k < MSGLEN; k++) {
        assert(a->block[k] == b->block[k]);
        assert(a->block[k] == msg[k]);
    }
    LEANC_REACH("compared");
    sponge_free(a);
    sponge_free(b);
}

int main(void)
{
    check_null_arguments();
    check_chunked_equality();
    LEANC_REACH("done");
    return 0;
}
