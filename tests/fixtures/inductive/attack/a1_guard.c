#include <assert.h>
unsigned nondet_uint(void);
int buf[4];
int g;
int f(unsigned n)
{
    unsigned i = 0;
    g = 0;
    while (i < n && (++g, 1))
        __CPROVER_assigns(i)
        __CPROVER_loop_invariant(i <= n)
        __CPROVER_decreases(n - i)
    {
        i++;
    }
    return buf[g];
}
void entry(void) { unsigned n = nondet_uint(); f(n); }
