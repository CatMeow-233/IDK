int step(void);
int sum(unsigned);
unsigned nondet_uint(void);
void entry(void) { step();
#ifdef WITH_LOOP
 sum(nondet_uint());
#endif
}
