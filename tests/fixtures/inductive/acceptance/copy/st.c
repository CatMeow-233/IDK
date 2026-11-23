static const int table[4] = {1, 2, 3, 4};
int counter = 5;
static int cache_valid;
int lookup(int i) { return table[i & 3]; }
int bump(void) { return ++counter; }
int cached(void) { return cache_valid; }
