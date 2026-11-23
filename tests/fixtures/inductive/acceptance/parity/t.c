#include "leanc.h"
#include <limits.h>
#include <string.h>
#ifdef __CPROVER__
int cprover_defined = 1;
#else
int cprover_defined = 0;
#endif
int f(int x) { LEANC_ASSUME(x > 0); LEANC_REACH("a"); return x; }
