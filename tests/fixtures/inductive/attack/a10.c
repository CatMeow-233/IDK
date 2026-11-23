#include <assert.h>
int main(void)
{
    assert(__CPROVER_max_malloc_size == ((__CPROVER_size_t)1 << 55));
    assert(__CPROVER_max_malloc_size == ((__CPROVER_size_t)1 << 47));
    return 0;
}
