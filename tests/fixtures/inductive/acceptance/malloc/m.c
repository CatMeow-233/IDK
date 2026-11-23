#include <stdlib.h>
#include <limits.h>
void prove(void) { int *p = malloc(4); __CPROVER_assert(p != NULL, "malloc cannot fail"); __CPROVER_assert(CHAR_MIN < 0, "char is signed"); free(p); }
