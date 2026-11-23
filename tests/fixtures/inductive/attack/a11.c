#include <stdlib.h>
size_t nondet_size_t(void);
void write_past_end(unsigned char *data, size_t len)
{
    data[len] = 1;
}
int main(void)
{
    size_t len = nondet_size_t();
#ifdef HUGE
    __CPROVER_assume(len >= ((size_t)1 << HUGE));
#endif
    unsigned char *data = malloc(len);
    __CPROVER_assume(data != NULL);
    write_past_end(data, len);
    free(data);
    return 0;
}
