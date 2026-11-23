static unsigned calls;
unsigned g_calls;
int buf[4];
struct st { int k; int *p; };
static struct st sst = {0, 0};
static const unsigned limit = 3;
int step(void)
{
    static unsigned local_calls;
    calls++;
    g_calls++;
    local_calls++;
    int r = 0;
    if (calls == 1000000u) r += buf[10];
    if (g_calls == 1000000u) r += buf[11];
    if (local_calls == 1000000u) r += buf[12];
    if (sst.k == 7) r += buf[13];
    if (limit != 3) r += buf[14];
    return r;
}
#ifdef WITH_LOOP
int sum(unsigned n)
{
    int t = 0;
    for (unsigned i = 0; i < n; i++)
        __CPROVER_assigns(i, t) __CPROVER_loop_invariant(i <= n) __CPROVER_decreases(n - i)
    { t = 0; }
    return t;
}
#endif
