#include <assert.h>
#include <stddef.h>

static int cleaned;

static void mark(int *p)
{
    cleaned = *p;
}

typedef unsigned int small_t __attribute__((mode(QI)));

int main(void)
{
    long l = 0;
    size_t z = 0;
    _Bool flag = 0;
    {
        __attribute__((cleanup(mark))) int k = 7;
        (void)k;
    }
    assert(_Generic(l, long: 1, long long: 2) == 1);
    assert(_Generic(z, unsigned long: 1, unsigned long long: 2) == 1);
    assert(__builtin_types_compatible_p(int (*)(int), int (*)()) == 1);
    assert(__builtin_classify_type(flag) == 4);
    assert(sizeof(small_t) == 1);
    assert(cleaned == 7);
    return 0;
}
