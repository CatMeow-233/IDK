#include <stddef.h>
#define REPEAT4(i) for (int i = 0; i < 4; i++)
int nondet_int(void);
unsigned sum_do(const unsigned char *p, unsigned n)
{
    unsigned total = 0;
    unsigned i = 0;
    if (n == 0) {
        return 0;
    }
    do {
        total += p[i] & 1u;
        i++;
    } while (i < n);
    return total;
}
unsigned nested(const unsigned char *p, unsigned rows, unsigned cols)
{
    unsigned hits = 0;
    for (unsigned r = 0; r < rows; r++) {
        for (unsigned c = 0; c < cols; c++)
            hits |= p[(size_t)r * cols + c];
    }
    return hits;
}
int goto_loop(int n)
{
    int i = 0;
again:
    if (i < n) {
        i++;
        goto again;
    }
    return i;
}
int macro_loop(void)
{
    int acc = 0;
    REPEAT4(j) { acc += j; }
    return acc;
}
int const_loop(void)
{
    int acc = 0;
    for (int j = 0; j < 4; j++) { acc += j; }
    return acc;
}
int while_in_string(void)
{
    const char *s = "while (1) for (;;) do";
    /* while (1) { } */
    return s[0];
}
