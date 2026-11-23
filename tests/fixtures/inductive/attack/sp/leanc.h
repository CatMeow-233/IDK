/*
 * leanc.h - support macros for Lean C verification harnesses.
 *
 * Include this from TRUSTED (human-approved) CBMC harnesses only.  Editable
 * sources are not allowed to use CBMC built-ins (see docs/trust-model.md).
 *
 *   LEANC_ASSUME(cond)   restrict the symbolic input space (precondition).
 *   LEANC_REACH("label") vacuity probe.  Lean C re-runs CBMC with
 *                        -DLEANC_VACUITY_CHECK, where every probe becomes
 *                        __CPROVER_assert(0, "leanc_reach:label").  CBMC must
 *                        report the probe as FAILED (= reachable); a probe that
 *                        "holds" means the code after it is unreachable under
 *                        the assumptions, i.e. the verification would be vacuous.
 *
 * Put at least one LEANC_REACH after the call to the function under
 * verification (and ideally one per interesting branch of the harness).
 */
#ifndef LEANC_H
#define LEANC_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __CPROVER__
#  ifdef LEANC_VACUITY_CHECK
#    define LEANC_REACH(label) __CPROVER_assert(0, "leanc_reach:" label)
#  else
#    define LEANC_REACH(label) ((void)0)
#  endif
#  define LEANC_ASSUME(cond) __CPROVER_assume(cond)
/* Functions without a body whose name starts with nondet_ return arbitrary values in CBMC. */
int nondet_int(void);
unsigned int nondet_uint(void);
long nondet_long(void);
unsigned long nondet_ulong(void);
long long nondet_llong(void);
size_t nondet_size_t(void);
char nondet_char(void);
unsigned char nondet_uchar(void);
short nondet_short(void);
bool nondet_bool(void);
int32_t nondet_int32_t(void);
uint32_t nondet_uint32_t(void);
int64_t nondet_int64_t(void);
uint64_t nondet_uint64_t(void);
#else
#  define LEANC_REACH(label) ((void)0)
#  define LEANC_ASSUME(cond) ((void)0)
#endif

#endif /* LEANC_H */
