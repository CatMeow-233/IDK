#include <assert.h>
#include <stdlib.h>

#include "leanc.h"
#include "iset.h"

int main(void)
{
    struct iset s;
    s.len = nondet_int();
    s.cap = nondet_int();
    LEANC_ASSUME(0 <= s.len && s.len <= s.cap && s.cap <= ISET_MAX_LEN);
    s.items = malloc((size_t)s.cap * sizeof(int));
    LEANC_ASSUME(s.items != NULL);
    int x = nondet_int();
    if (nondet_bool()) {
        int r = iset_rank(&s, x);
        LEANC_REACH("rank_returned");
        assert(0 <= r && r <= s.len);
    } else {
        int r = iset_contains(&s, x);
        LEANC_REACH("contains_returned");
        assert(r == 0 || r == 1);
    }
    assert(0 <= s.len && s.len <= s.cap && s.cap <= ISET_MAX_LEN);
    free(s.items);
    return 0;
}
