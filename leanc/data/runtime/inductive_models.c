typedef __CPROVER_size_t leanc_model_size_t;

void *memcpy(void *dst, const void *src, leanc_model_size_t n)
{
    __CPROVER_assert(
        __CPROVER_POINTER_OBJECT(dst) != __CPROVER_POINTER_OBJECT(src) ||
        (const char *)src >= (const char *)dst + n || (const char *)dst >= (const char *)src + n,
        "leanc_model: memcpy src/dst overlap");
    __CPROVER_assert(__CPROVER_r_ok(src, n), "leanc_model: memcpy source region readable");
    __CPROVER_assert(__CPROVER_w_ok(dst, n), "leanc_model: memcpy destination region writeable");
    if (n > 0) {
        __CPROVER_havoc_slice(dst, n);
    }
    return dst;
}

void *memmove(void *dst, const void *src, leanc_model_size_t n)
{
    __CPROVER_assert(__CPROVER_r_ok(src, n), "leanc_model: memmove source region readable");
    __CPROVER_assert(__CPROVER_w_ok(dst, n), "leanc_model: memmove destination region writeable");
    if (n > 0) {
        __CPROVER_havoc_slice(dst, n);
    }
    return dst;
}

void *memset(void *dst, int value, leanc_model_size_t n)
{
    (void)value;
    __CPROVER_assert(__CPROVER_w_ok(dst, n), "leanc_model: memset destination region writeable");
    if (n > 0) {
        __CPROVER_havoc_slice(dst, n);
    }
    return dst;
}
