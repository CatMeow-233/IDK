int step(void);
int sum(unsigned);
unsigned nondet_uint(void);
int main(void) { step();
#ifdef WITH_LOOP
 sum(nondet_uint());
#endif
 return 0; }
