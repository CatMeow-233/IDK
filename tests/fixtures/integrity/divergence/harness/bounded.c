#include <assert.h>
#include "leanc.h"
#include "t.h"

int main(void)
{
    struct buf b;
    b.n = nondet_uint();
    LEANC_ASSUME(b.n <= BUF);
    size_t len = nondet_size_t();
    LEANC_ASSUME(len <= 4);
    unsigned char src[4] = {0, 1, 2, 3};
    int rc = t_fill(&b, src, len);
    LEANC_REACH("ret");
    assert(rc == 0 || rc == -1);
    return 0;
}
