int helper(void);
void from_models(void) { __CPROVER_assert(helper() == 1, "models call binds to the harness global"); }
