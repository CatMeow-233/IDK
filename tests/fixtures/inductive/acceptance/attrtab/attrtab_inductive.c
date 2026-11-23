#include <assert.h>
#include <stdint.h>
#include <stdlib.h>

#include "leanc.h"
#include "attrtab.h"

void prove_attrtab_clone(void)
{
    struct elemtype src;
    src.natts = nondet_uint32_t();
    src.atts = malloc((size_t)src.natts * sizeof(struct defatt));
    LEANC_ASSUME(src.atts != NULL);

    struct elemtype dst;
    dst.atts = NULL;
    dst.natts = 0;

    int rc = attrtab_clone(&src, &dst);
    LEANC_REACH("clone_returned");
    assert(rc == ATTR_OK || rc == ATTR_ERR_OVERFLOW);
    if (rc == ATTR_OK) {
        LEANC_REACH("clone_ok");
        assert(dst.natts == src.natts);
        assert(dst.natts == 0 || dst.atts != NULL);
    } else {
        LEANC_REACH("clone_overflow");
        assert(dst.atts == NULL && dst.natts == 0);
    }
    free(dst.atts);
    free(src.atts);
}
