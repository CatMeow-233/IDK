#include <stddef.h>
#include <string.h>
static void *leanc_inductive_memcpy(void *dst, const void *src, size_t n) { (void)src; (void)n; return dst; }
unsigned char tab[1];
int buf[4];
unsigned char src[4] = {9, 9, 9, 9};
int main(void)
{
    tab[0] = 0;
    memcpy(tab, src, 1); (void)leanc_inductive_memcpy;
    return buf[tab[0]];
}
