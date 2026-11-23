#include <stddef.h>
#include <assert.h>
#include "leanc.h"
int main(void)
{
    size_t declared = nondet_size_t();
    unsigned long long undeclared = nondet_ulonglong();
    assert(declared != ((size_t)1 << 40));
    assert(undeclared != (1ull << 40));
    return 0;
}
