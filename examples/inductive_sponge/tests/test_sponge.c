/* Trusted runtime tests for the sponge (run under ASan/UBSan/LSan). */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "sponge.h"

#define LONG_MSG 1000

static int check(int cond, const char *what)
{
    if (!cond) {
        fprintf(stderr, "FAILED: %s\n", what);
        return 1;
    }
    return 0;
}

static void fill_message(uint8_t *msg, size_t len)
{
    for (size_t k = 0; k < len; k++) {
        msg[k] = (uint8_t)(k * 31u + 7u);
    }
}

static int digest_of_pieces(const uint8_t *msg, const size_t *pieces, size_t count, uint8_t out[SPONGE_OUT])
{
    struct sponge *s = sponge_new();
    if (s == NULL) {
        return SPONGE_ERR_NOMEM;
    }
    size_t at = 0;
    int rc = SPONGE_OK;
    for (size_t k = 0; k < count && rc == SPONGE_OK; k++) {
        rc = sponge_absorb(s, msg + at, pieces[k]);
        at += pieces[k];
    }
    if (rc == SPONGE_OK) {
        rc = sponge_finalize(s, out);
    }
    sponge_free(s);
    return rc;
}

static int test_full_blocks(void)
{
    uint8_t *msg = malloc(3 * SPONGE_RATE + 5);
    if (msg == NULL) {
        return 1;
    }
    fill_message(msg, 3 * SPONGE_RATE + 5);
    struct sponge *s = sponge_new();
    int f = check(s != NULL, "sponge_new");
    if (s != NULL) {
        f |= check(sponge_absorb(s, msg, SPONGE_RATE) == SPONGE_OK, "one block");
        f |= check(s->fill == 0 && s->rounds == 1, "one block: one round, empty buffer");
        f |= check(sponge_absorb(s, msg + SPONGE_RATE, 2 * SPONGE_RATE + 5) == SPONGE_OK, "two blocks and a tail");
        f |= check(s->fill == 5 && s->rounds == 3, "tail of 5 bytes buffered after 3 rounds");
        f |= check(memcmp(s->block, msg + 3 * SPONGE_RATE, 5) == 0, "tail bytes buffered in order");
    }
    sponge_free(s);
    free(msg);
    return f;
}

static int test_chunked(void)
{
    uint8_t *msg = malloc(LONG_MSG);
    if (msg == NULL) {
        return 1;
    }
    fill_message(msg, LONG_MSG);
    const size_t whole[] = {LONG_MSG};
    const size_t bytes[] = {1, 143, 7, 137, 145, 288, 0, 279};
    const size_t blocks[] = {SPONGE_RATE, SPONGE_RATE, LONG_MSG - 2 * SPONGE_RATE};
    uint8_t a[SPONGE_OUT];
    uint8_t b[SPONGE_OUT];
    uint8_t c[SPONGE_OUT];
    int f = check(digest_of_pieces(msg, whole, 1, a) == SPONGE_OK, "one-shot digest");
    f |= check(digest_of_pieces(msg, bytes, sizeof bytes / sizeof bytes[0], b) == SPONGE_OK, "uneven pieces");
    f |= check(digest_of_pieces(msg, blocks, sizeof blocks / sizeof blocks[0], c) == SPONGE_OK, "block pieces");
    f |= check(memcmp(a, b, SPONGE_OUT) == 0, "uneven pieces give the one-shot digest");
    f |= check(memcmp(a, c, SPONGE_OUT) == 0, "block pieces give the one-shot digest");
    free(msg);
    return f;
}

static int test_finalize(void)
{
    uint8_t msg[SPONGE_RATE - 1];
    fill_message(msg, sizeof msg);
    const size_t almost_full[] = {sizeof msg};
    const size_t empty[] = {0};
    uint8_t first[SPONGE_OUT];
    uint8_t again[SPONGE_OUT];
    uint8_t none[SPONGE_OUT];
    int f = check(digest_of_pieces(msg, almost_full, 1, first) == SPONGE_OK, "finalize with 143 buffered bytes");
    f |= check(digest_of_pieces(msg, almost_full, 1, again) == SPONGE_OK, "finalize again");
    f |= check(memcmp(first, again, SPONGE_OUT) == 0, "finalize is deterministic");
    f |= check(digest_of_pieces(msg, empty, 1, none) == SPONGE_OK, "finalize of the empty message");
    f |= check(memcmp(first, none, SPONGE_OUT) != 0, "different messages, different digests");
    return f;
}

static int test_null_args(void)
{
    uint8_t buf[1] = {0};
    uint8_t out[SPONGE_OUT];
    int f = check(sponge_absorb(NULL, buf, 1) == SPONGE_ERR_NULL, "absorb into NULL");
    f |= check(sponge_finalize(NULL, out) == SPONGE_ERR_NULL, "finalize NULL");
    struct sponge *s = sponge_new();
    f |= check(s != NULL, "sponge_new");
    if (s != NULL) {
        f |= check(sponge_absorb(s, NULL, 1) == SPONGE_ERR_NULL, "NULL data with len 1");
        f |= check(sponge_absorb(s, NULL, 0) == SPONGE_OK && s->fill == 0, "NULL data with len 0");
        f |= check(sponge_finalize(s, NULL) == SPONGE_ERR_NULL, "finalize into NULL");
    }
    sponge_free(s);
    sponge_free(NULL);
    return f;
}

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "usage: %s <case>\n", argv[0]);
        return 2;
    }
    if (strcmp(argv[1], "full_blocks") == 0) return test_full_blocks();
    if (strcmp(argv[1], "chunked") == 0) return test_chunked();
    if (strcmp(argv[1], "finalize") == 0) return test_finalize();
    if (strcmp(argv[1], "null_args") == 0) return test_null_args();
    fprintf(stderr, "unknown case %s\n", argv[1]);
    return 2;
}
