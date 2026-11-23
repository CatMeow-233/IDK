#include <string.h>
int nondet_int(void);
int g;
int main(void) {
    int n = nondet_int(); __CPROVER_assume(n >= 0);
    g = 1;
    for (int i = 0; i < n; i++) __CPROVER_assigns(i) __CPROVER_loop_invariant(0 <= i && i <= n) __CPROVER_decreases(n - i) { if (i > 0) memset(&g, 0, sizeof g); }
    __CPROVER_assert(g == 1, "g untouched (FALSE in reality when n>1)");
    return 0;
}
