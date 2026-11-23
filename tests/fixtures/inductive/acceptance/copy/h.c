struct msg { unsigned char pos; unsigned char table[16]; };
int pick(struct msg *m, const unsigned char *wire);
unsigned char nondet_uchar(void);
void prove_pick(void) { struct msg m; unsigned char w = nondet_uchar(); pick(&m, &w); }
