void *memcpy(void *dst, const void *src, __CPROVER_size_t n)
{
    __CPROVER_assert(__CPROVER_r_ok(src, n), "src readable");
    __CPROVER_assert(__CPROVER_w_ok(dst, n), "dst writeable");
    return dst;
}
