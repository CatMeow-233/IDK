#include <stddef.h>
void *memset(void *s, int c, size_t n)
{
    __CPROVER_assert(__CPROVER_w_ok(s, n), "memset destination writable for n bytes");
    __CPROVER_havoc_slice(s, n);
    return s;
}
void *memcpy(void *dst, const void *src, size_t n)
{
    __CPROVER_assert(__CPROVER_w_ok(dst, n), "memcpy destination writable for n bytes");
    __CPROVER_assert(__CPROVER_r_ok(src, n), "memcpy source readable for n bytes");
    __CPROVER_havoc_slice(dst, n);
    return dst;
}
void *memmove(void *dst, const void *src, size_t n)
{
    __CPROVER_assert(__CPROVER_w_ok(dst, n), "memmove destination writable for n bytes");
    __CPROVER_assert(__CPROVER_r_ok(src, n), "memmove source readable for n bytes");
    __CPROVER_havoc_slice(dst, n);
    return dst;
}
