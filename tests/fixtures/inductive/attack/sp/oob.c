#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "sponge.h"

static void sponge_mix(struct sponge *s)
{
    for (uint32_t k = 0; k < SPONGE_RATE; k++) __CPROVER_assigns(k, __CPROVER_object_upto(s->acc, 28)) __CPROVER_loop_invariant(k <= 144) __CPROVER_decreases(144 - k) {
        uint32_t t = (uint32_t)(s->acc[k % SPONGE_OUT] + s->block[k] + s->rounds + k);
        s->acc[k % SPONGE_OUT] = (uint8_t)((t << 1) | (t >> 7));
    }
    s->rounds++;
}

struct sponge *sponge_new(void)
{
    struct sponge *s = malloc(sizeof(struct sponge));
    if (s == NULL) {
        return NULL;
    }
    memset(s->block, 0, SPONGE_RATE);
    memset(s->acc, 0, SPONGE_OUT);
    s->fill = 0;
    s->rounds = 0;
    return s;
}

int sponge_absorb(struct sponge *s, const uint8_t *data, size_t len)
{
    if (s == NULL || (data == NULL && len != 0)) {
        return SPONGE_ERR_NULL;
    }
    size_t i = 0;
    while (i < len) __CPROVER_assigns(i, __CPROVER_object_whole(s)) __CPROVER_loop_invariant((i <= len) && (s->fill < 144)) __CPROVER_decreases(len - i) {
        if (s->fill == 0 && len >= i + SPONGE_RATE) {
            memcpy(s->block, data + i, SPONGE_RATE);
            sponge_mix(s);
            i += SPONGE_RATE;
        } else {
            uint32_t partial = (uint32_t)(len - i);
            if (partial == 0) {
                partial = 1;
            }
            if (partial + s->fill > SPONGE_RATE) {
                partial = SPONGE_RATE - s->fill;
            }
            memcpy(s->block + s->fill, data + i, partial);
            s->fill += partial;
            i += partial;
            if (s->fill == SPONGE_RATE) {
                sponge_mix(s);
                s->fill = 0;
            }
        }
    }
    return SPONGE_OK;
}

int sponge_finalize(struct sponge *s, uint8_t out[SPONGE_OUT])
{
    if (s == NULL || out == NULL) {
        return SPONGE_ERR_NULL;
    }
    s->block[s->fill] = 0x06;
    for (uint32_t k = s->fill + 1; k < SPONGE_RATE; k++) __CPROVER_assigns(k, __CPROVER_object_upto(s->block, 144)) __CPROVER_loop_invariant(k <= 144) __CPROVER_decreases(144 - k) {
        s->block[k] = 0;
    }
    s->block[SPONGE_RATE - 1] = (uint8_t)(s->block[SPONGE_RATE - 1] ^ 0x80);
    sponge_mix(s);
    memcpy(out, s->acc, SPONGE_OUT);
    return SPONGE_OK;
}

void sponge_free(struct sponge *s)
{
    free(s);
}
