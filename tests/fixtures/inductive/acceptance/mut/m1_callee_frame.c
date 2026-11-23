int nondet_int(void);
int g;
static void touch(void) { g = 0; }
int main(void) {
    int n = nondet_int(); __CPROVER_assume(n >= 0);
    g = 1;
    for (int i = 0; i < n; i++) __CPROVER_assigns(i) __CPROVER_loop_invariant(0 <= i && i <= n) __CPROVER_decreases(n - i) { touch(); }
    __CPROVER_assert(g == 1, "g untouched (FALSE in reality when n>0)");
    return 0;
}
