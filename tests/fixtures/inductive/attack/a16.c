#include <string.h>
struct st { unsigned char block[4]; unsigned char acc[4]; };
unsigned char src[8];
int main(void) { struct st s; s.acc[0] = 0; memcpy(s.block, src, 5); s.block[4] = 1; return 0; }
