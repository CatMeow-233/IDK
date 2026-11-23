#include <string.h>
int main(void){char a[4],b[4]; unsigned n; __CPROVER_assume(n<=4); memcpy(a,b,n); memmove(a,b,n); memset(a,0,n); return 0;}
