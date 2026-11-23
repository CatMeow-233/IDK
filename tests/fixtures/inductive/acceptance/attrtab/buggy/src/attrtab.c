#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "attrtab.h"

#define ATT_STRIDE ((uint32_t)sizeof(struct defatt))

int attrtab_clone(const struct elemtype *src, struct elemtype *dst)
{
    if (src == NULL || dst == NULL) {
        return ATTR_ERR_NULL;
    }
    if (src->natts == 0) {
        dst->atts = NULL;
        dst->natts = 0;
        return ATTR_OK;
    }
    uint32_t bytes = (uint32_t)(src->natts * ATT_STRIDE);
    struct defatt *copy = malloc(bytes);
    if (copy == NULL) {
        return ATTR_ERR_NOMEM;
    }
    memcpy(copy, src->atts, (size_t)src->natts * sizeof(struct defatt));
    dst->atts = copy;
    dst->natts = src->natts;
    return ATTR_OK;
}

void attrtab_free(struct elemtype *e)
{
    if (e != NULL) {
        free(e->atts);
        e->atts = NULL;
        e->natts = 0;
    }
}
