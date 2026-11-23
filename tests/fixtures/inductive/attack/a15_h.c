#include <assert.h>
int use_ed(void);
int helper(void) { return 1; }
int main(void) { assert(helper() == 1); assert(use_ed() == 0); return 0; }
