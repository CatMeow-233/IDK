int nondet_int(void);
int g;
int bump(void) { return ++g; }
int (*fp)(void) = bump;
struct pair { int a; int b; };
void prove(void)
{
    int n = nondet_int(); __CPROVER_assume(n >= 0);
    int i = 0;
    while (i < n) __CPROVER_assigns(i) __CPROVER_loop_invariant(0 <= i && i <= n && ((struct pair){ .a = (g = 7), .b = 0 }).b == 0) __CPROVER_decreases(n - i)
    {
        i++;
    }
    __CPROVER_assert(i == n, "exit");
}
