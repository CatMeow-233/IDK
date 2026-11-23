#include <string.h>
unsigned char small[4];
unsigned char src[64];
size_t nondet_size_t(void);
void b_copy(void) { size_t n = nondet_size_t(); __CPROVER_assume(n <= 64); memcpy(small, src, n); }
