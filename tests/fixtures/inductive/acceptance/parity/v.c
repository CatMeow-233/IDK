long stdc_version = __STDC_VERSION__;
#ifdef __CPROVER__
int cp = 1;
#else
int cp = 0;
#endif
#ifdef __GNUC__
int gnuc = __GNUC__;
#endif
#ifdef __clang__
int is_clang = 1;
#endif
#ifdef __OPTIMIZE__
int opt = 1;
#endif
