#include <stddef.h>
#include <string.h>
unsigned nondet_uint(void);
unsigned char tab[1];
int buf[4];
unsigned char src[64];
int f(unsigned n)
{
    tab[0] = 0;
    unsigned i = 0;
#ifdef FNPTR
    void *(*cp)(void *, const void *, size_t) = memcpy;
#endif
    while (i < n)
        __CPROVER_assigns(i)
        __CPROVER_loop_invariant(i <= n)
        __CPROVER_decreases(n - i)
    {
#if defined(BUILTIN)
        __builtin_memcpy(tab, src + (i % 32), 1);
#elif defined(CHK)
        __builtin___memcpy_chk(tab, src + (i % 32), 1, 1);
#elif defined(FNPTR)
        cp(tab, src + (i % 32), 1);
#else
        memcpy(tab, src + (i % 32), 1);
#endif
        i++;
    }
    return buf[tab[0]];
}
void entry(void) { f(nondet_uint()); }
int main(void) { f(nondet_uint()); return 0; }
