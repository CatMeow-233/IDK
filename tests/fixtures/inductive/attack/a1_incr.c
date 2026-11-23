#include <assert.h>
unsigned nondet_uint(void);
int buf[4];
int g;
int f(unsigned n)
{
    g = 0;
    for (unsigned i = 0; i < n; i++, g++)
        __CPROVER_assigns(i)
        __CPROVER_loop_invariant(i <= n)
        __CPROVER_decreases(n - i)
    {
    }
    return buf[g];
}
void entry(void) { unsigned n = nondet_uint(); f(n); }
