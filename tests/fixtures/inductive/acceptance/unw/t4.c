
unsigned nondet_uint(void);
void prove(void)
{
    unsigned n = nondet_uint();
    unsigned i = 0; unsigned v0 = 0; unsigned v1 = 0; unsigned v2 = 0; unsigned v3 = 0;
    while (i < n) __CPROVER_assigns(i, v0, v1, v2, v3) __CPROVER_loop_invariant(i <= n) __CPROVER_decreases(n - i)
    {
        v0 = i; v1 = i; v2 = i; v3 = i;
        for (unsigned j = 0; j < 3; j++) __CPROVER_assigns(j, v0, v1, v2, v3) __CPROVER_loop_invariant(j <= 3 && i < n) __CPROVER_decreases(3 - j) { v0 = i; v1 = i; v2 = i; v3 = i; }
        i++;
    }
}
