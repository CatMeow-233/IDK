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
    int len = s.len;
    int rc = iset_insert(&s, nondet_int());
    LEANC_REACH("insert_returned");
    assert(rc == ISET_OK || rc == ISET_PRESENT || rc == ISET_ERR_TOO_BIG);
    assert(s.len == len + (rc == ISET_OK));
    assert(0 <= s.len && s.len <= s.cap && s.cap <= ISET_MAX_LEN);
    free(s.items);
    return 0;
}
