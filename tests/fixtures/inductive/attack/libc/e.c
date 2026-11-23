#include <string.h>
#include <stdlib.h>
size_t f(const char *s) { return strlen(s); }
int g(int a) { return abs(a); }
void *h(size_t n) { void *p = malloc(n); free(p); return 0; }
