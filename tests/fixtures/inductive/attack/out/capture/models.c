#include <stddef.h>
#include <stdlib.h>
static int leanc_inductive_static_canary = 1;
void *memcpy(void *dst, const void *src, size_t n)
{
    __CPROVER_assert(__CPROVER_r_ok(src, n), "leanc_model:memcpy source readable for n bytes");
    __CPROVER_assert(__CPROVER_w_ok(dst, n), "leanc_model:memcpy destination writable for n bytes");
    __CPROVER_assert(n == 0 || !__CPROVER_same_object(dst, src) || (const char *)dst + n <= (const char *)src || (const char *)src + n <= (const char *)dst, "leanc_model:memcpy regions do not overlap");
    __CPROVER_havoc_slice(dst, n);
    return dst;
}
void *memmove(void *dst, const void *src, size_t n)
{
    __CPROVER_assert(__CPROVER_r_ok(src, n), "leanc_model:memmove source readable for n bytes");
    __CPROVER_assert(__CPROVER_w_ok(dst, n), "leanc_model:memmove destination writable for n bytes");
    __CPROVER_havoc_slice(dst, n);
    return dst;
}
void *memset(void *dst, int c, size_t n)
{
    (void)c;
    __CPROVER_assert(__CPROVER_w_ok(dst, n), "leanc_model:memset destination writable for n bytes");
    __CPROVER_havoc_slice(dst, n);
    return dst;
}
void prove_iset_rank(void);
void leanc_inductive_entry(void)
{
    __CPROVER_assert(leanc_inductive_static_canary == 1, "leanc_canary:static objects start arbitrary");
    prove_iset_rank();
    free((void *)0);
}
