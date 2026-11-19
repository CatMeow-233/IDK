/* Harness with struct / array / bool inputs (counterexample rendering). */
#include <assert.h>

#include "leanc.h"
#include "cbmc_fx.h"

int main(void)
{
    struct point q;
    q.x = 5;
    q.y = nondet_int();
    q.tag[0] = 'a';
    int arr[3] = {1, 2, 3};
    arr[1] = nondet_int();
    int k = nondet_int();
    bool flag = nondet_bool();
    int r = point_ratio(&q, k);
    LEANC_REACH("after_call");
    assert(r != 7);
    (void)arr;
    (void)flag;
    return 0;
}
