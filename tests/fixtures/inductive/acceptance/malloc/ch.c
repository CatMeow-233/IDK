void prove(void) { char c = (char)200; __CPROVER_assert(c < 0, "plain char is signed (type level)"); }
