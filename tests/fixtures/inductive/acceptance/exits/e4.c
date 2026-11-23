unsigned nondet_uint(void);
void *malloc(__CPROVER_size_t);
int g;
static int find(const unsigned char *p, unsigned n, unsigned char key)
{
    for (unsigned i = 0; i < n; i++) __CPROVER_assigns(i, g) __CPROVER_loop_invariant(i <= n && (g == 0 || g == 1)) __CPROVER_decreases(n - i)
    {
        if (p[i] == key) {
            g = 1;
            break;
        }
        if (p[i] == 255) { continue; }
    }
    return -1;
}
void prove(void)
{
    unsigned n = nondet_uint();
    unsigned char *p = malloc(n);
    __CPROVER_assume(p != 0);
    g = 0;
    int r = find(p, n, 7);
    __CPROVER_assert(g == 0, "post");
}
