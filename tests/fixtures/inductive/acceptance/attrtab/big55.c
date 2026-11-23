void *malloc(__CPROVER_size_t);
__CPROVER_size_t nondet_size_t(void);
void prove(void)
{
    __CPROVER_size_t n = nondet_size_t();
    __CPROVER_assume(n == ((__CPROVER_size_t)1 << 55));
    unsigned char *p = malloc(n);
    __CPROVER_assume(p != 0);
    p[n - 1] = 1;
    __CPROVER_assert(p[n - 1] == 1, "last byte holds its value");
    __CPROVER_assert(n != ((__CPROVER_size_t)1 << 55), "reach (must FAIL)");
}
