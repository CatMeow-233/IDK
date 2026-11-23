#ifndef SPONGE_H
#define SPONGE_H

#include <stddef.h>
#include <stdint.h>

#define SPONGE_RATE 144
#define SPONGE_OUT 28

#define SPONGE_OK 0
#define SPONGE_ERR_NULL (-1)
#define SPONGE_ERR_NOMEM (-3)

struct sponge {
    uint8_t block[SPONGE_RATE];
    uint8_t acc[SPONGE_OUT];
    uint32_t fill;
    uint32_t rounds;
};

struct sponge *sponge_new(void);
int sponge_absorb(struct sponge *s, const uint8_t *data, size_t len);
int sponge_finalize(struct sponge *s, uint8_t out[SPONGE_OUT]);
void sponge_free(struct sponge *s);

#endif
