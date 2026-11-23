unsigned nondet_uint(void);
int buf[4];
int f(unsigned n)
{
    unsigned i = 0;
    unsigned j = 0;
    while (i < n)
        __CPROVER_assigns(i)
        __CPROVER_loop_invariant((i <= n) && (&j != 0) && (&i != 0))
        __CPROVER_decreases(n - i)
    {
        j = j + 1;
        i++;
    }
    return buf[j];
}
int main(void) { f(nondet_uint()); return 0; }
