#include <stddef.h>
void *leanc_inductive_memcpy(void *dst, const void *src, size_t n)
{
    __CPROVER_assert(__CPROVER_w_ok(dst, n), "leanc_mem:memcpy destination writable for n bytes");
    __CPROVER_assert(__CPROVER_r_ok(src, n), "leanc_mem:memcpy source readable for n bytes");
    __CPROVER_havoc_slice(dst, n);
    return dst;
}
void *leanc_inductive_memmove(void *dst, const void *src, size_t n) { __CPROVER_havoc_slice(dst, n); return dst; }
void *leanc_inductive_memset(void *dst, int value, size_t n) { (void)value; __CPROVER_havoc_slice(dst, n); return dst; }
