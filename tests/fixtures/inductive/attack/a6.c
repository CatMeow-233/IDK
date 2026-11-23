#include <stdlib.h>
#include <stdint.h>
size_t nondet_size_t(void);
int buf[4];
int f(const unsigned char *data, size_t len)
{
    if (len >= ((size_t)1 << 56)) return buf[10];
    if (len >= ((size_t)1 << 47)) return buf[11];
    return data[len - 1];
}
int main(void)
{
    size_t len = nondet_size_t();
    unsigned char *data = malloc(len);
    __CPROVER_assume(data != NULL);
    __CPROVER_assume(len > 0);
    f(data, len);
    return 0;
}
