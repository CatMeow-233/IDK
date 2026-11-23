#include <string.h>
#include <stdlib.h>
unsigned nondet_uint(void);
unsigned char tab[4];
int buf[4];
unsigned char src[16] = {9, 0};
int f(unsigned n)
{
    tab[0] = 0;
    unsigned i = 0;
    while (i < n)
        __CPROVER_assigns(i)
        __CPROVER_loop_invariant(i <= n)
        __CPROVER_decreases(n - i)
    {
        strncat((char *)tab, (const char *)src, 1);
        i++;
    }
    return buf[tab[0] & 3];
}
int main(void) { f(nondet_uint()); return 0; }
