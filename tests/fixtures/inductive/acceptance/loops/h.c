int const_loop(void);
int macro_loop(void);
int goto_loop(int n);
unsigned sum_do(const unsigned char *p, unsigned n);
unsigned nested(const unsigned char *p, unsigned rows, unsigned cols);
int nondet_int(void);
unsigned nondet_uint(void);
void *malloc(__CPROVER_size_t);
void prove_const(void) { int r = const_loop(); __CPROVER_assert(r == 6, "sum"); }
void prove_goto(void) { int n = nondet_int(); __CPROVER_assume(n >= 0 && n <= 3); int r = goto_loop(n); __CPROVER_assert(r == n, "goto"); }
void prove_do(void) { unsigned n = nondet_uint(); unsigned char *p = malloc(n); __CPROVER_assume(p != 0); unsigned r = sum_do(p, n); __CPROVER_assert(r <= n, "do"); }
void prove_nested(void) { unsigned rows = nondet_uint(), cols = nondet_uint(); __CPROVER_assume((__CPROVER_size_t)rows * cols <= 4000000000u); unsigned char *p = malloc((__CPROVER_size_t)rows * cols); __CPROVER_assume(p != 0); nested(p, rows, cols); }
