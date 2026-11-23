int nondet_int(void);
int g;
void ext_touch(int *p);
int main(void) {
    int n = nondet_int(); __CPROVER_assume(n >= 0);
    g = 1;
    for (int i = 0; i < n; i++) __CPROVER_assigns(i) __CPROVER_loop_invariant(0 <= i && i <= n) __CPROVER_decreases(n - i) { ext_touch(&g); }
    __CPROVER_assert(g == 1, "g untouched");
    return 0;
}
