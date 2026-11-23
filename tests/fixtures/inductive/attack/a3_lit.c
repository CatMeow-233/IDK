#include <stdlib.h>
#include <string.h>
static unsigned char src[16];
void entry(void)
{
#if V_uaf
    char *p = malloc(4);
    __CPROVER_assume(p != NULL);
    free(p);
    memcpy(p, src, 4);
#elif V_dead
    char *q;
    { char t[4]; q = t; }
    memcpy(q, src, 4);
#else
    char *l = (char *)"abcd";
    memcpy(l, src, 4);
#endif
}
