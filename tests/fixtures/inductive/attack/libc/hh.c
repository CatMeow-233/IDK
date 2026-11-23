#include <assert.h>
#include <stddef.h>
size_t f(const char *s);
int g(int a);
void *h(size_t n);
int nondet_int(void);
int main(void) { char b[4] = "abc"; assert(f(b) == 3); int a = nondet_int(); if (a > -100 && a < 100) assert(g(a) >= 0); h(4); return 0; }
