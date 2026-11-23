#include <string.h>
struct msg { unsigned char pos; unsigned char table[16]; };
int pick(struct msg *m, const unsigned char *wire)
{
    m->pos = 0;
    memcpy(&m->pos, wire, 1);
    return m->table[m->pos];
}
