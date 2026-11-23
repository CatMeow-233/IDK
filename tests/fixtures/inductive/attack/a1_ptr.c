unsigned nondet_uint(void);
int buf[4];
int f(unsigned n)
{
    int x = 0;
    int *p = &x;
    unsigned i = 0;
    while (i < n)
        __CPROVER_assigns(i)
        __CPROVER_loop_invariant(i <= n)
        __CPROVER_decreases(n - i)
    {
        *p = *p + 1;
        i++;
    }
    return buf[x];
}
void entry(void) { unsigned n = nondet_uint(); f(n); }
