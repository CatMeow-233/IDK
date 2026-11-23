int lookup(int i); int bump(void); int cached(void); int nondet_int(void);
extern int counter;
void prove_statics(void)
{
    __CPROVER_assert(lookup(nondet_int()) >= 1, "const table keeps its initialiser");
    __CPROVER_assert(counter == 5, "mutable global keeps its initialiser");
    __CPROVER_assert(cached() == 0, "static zero-initialised");
}
