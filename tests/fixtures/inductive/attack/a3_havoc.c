#include <stdlib.h>
#include <string.h>
#include <assert.h>
unsigned char nondet_uchar(void);
size_t nondet_size_t(void);
struct two { char a[4]; int b; };
static unsigned char src[16];
void entry(void)
{
    struct two s;
    s.b = 0;
    memcpy(s.a, src, 8);
    assert(s.b == 0);

    int arr[4] = {0, 0, 0, 0};
    memcpy((char *)arr + 1, src, 2);
    assert(arr[0] == 0);
    assert(arr[1] == 0);

    char buf[8] = {0};
    size_t n = nondet_size_t();
    __CPROVER_assume(n <= 8);
    memcpy(buf, src, n);
    size_t j = nondet_size_t();
    __CPROVER_assume(j < n);
    assert(buf[j] == 0);
    size_t k = nondet_size_t();
    __CPROVER_assume(k >= n && k < 8);
    assert(buf[k] == 0);

    char *p = malloc(4);
    __CPROVER_assume(p != NULL);
    free(p);
    memcpy(p, src, 4);

    char *q;
    { char t[4]; q = t; }
    memcpy(q, src, 4);
}
