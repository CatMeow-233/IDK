#include <stdlib.h>
size_t nondet_size_t(void);
int main(void)
{
    size_t k = nondet_size_t();
    unsigned char *data = malloc(16);
    __CPROVER_assume(data != NULL);
    __CPROVER_assume(k == ((size_t)1 << 56) + 1 || k == ((size_t)1 << 48) + 1);
    data[k] = 1;
    free(data);
    return 0;
}
