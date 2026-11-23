#ifndef ISET_H
#define ISET_H

#include <limits.h>
#include <stddef.h>

#define ISET_OK 0
#define ISET_PRESENT 1
#define ISET_ABSENT 2
#define ISET_ERR_NULL (-1)
#define ISET_ERR_RANGE (-2)
#define ISET_ERR_NOMEM (-3)
#define ISET_ERR_TOO_BIG (-4)

#define ISET_MAX_LEN INT_MAX

struct iset {
    int *items;
    int len;
    int cap;
};

int iset_init(struct iset *s);
int iset_reserve(struct iset *s, int want);
int iset_insert(struct iset *s, int x);
int iset_remove(struct iset *s, int x);
int iset_contains(const struct iset *s, int x);
int iset_rank(const struct iset *s, int x);
int iset_len(const struct iset *s);
int iset_at(const struct iset *s, int i, int *out);
void iset_destroy(struct iset *s);

#endif
