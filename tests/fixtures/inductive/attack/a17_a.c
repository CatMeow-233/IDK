#include <stddef.h>
#define CPY memcpy
static void *CPY(void *d, const void *s, size_t n) { (void)s; (void)n; return d; }
int a_helper(void) { char x; return *(char *)CPY(&x, &x, 0) == x; }
