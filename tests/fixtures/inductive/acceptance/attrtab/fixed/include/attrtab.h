#ifndef ATTRTAB_H
#define ATTRTAB_H

#include <stddef.h>
#include <stdint.h>

#define ATTR_OK 0
#define ATTR_ERR_NULL (-1)
#define ATTR_ERR_NOMEM (-2)
#define ATTR_ERR_OVERFLOW (-3)

struct defatt {
    uint8_t name[4];
    uint32_t flags;
};

struct elemtype {
    struct defatt *atts;
    uint32_t natts;
};

int attrtab_clone(const struct elemtype *src, struct elemtype *dst);
void attrtab_free(struct elemtype *e);

#endif
