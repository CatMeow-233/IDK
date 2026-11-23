#include <assert.h>

#include "leanc.h"
#include "cap.h"

int helper(void) { return 1; }

int main(void)
{
    int r = use_ed();
    LEANC_REACH("used");
    assert(helper() == 1);
    assert(r == 0);
    return 0;
}
