#ifndef CLAMP_H
#define CLAMP_H

/* Status codes returned by clamp_int. */
#define CLAMP_OK 0
#define CLAMP_ERR_NULL (-1)
#define CLAMP_ERR_RANGE (-2)

/*
 * Clamp x into the closed interval [lo, hi].
 *   returns CLAMP_OK        if out != NULL and lo <= hi; *out = max(lo, min(x, hi))
 *           CLAMP_ERR_RANGE if out != NULL and lo > hi (empty interval); *out untouched
 *           CLAMP_ERR_NULL  if out == NULL (checked first; nothing is written)
 * Every int input is valid; no arithmetic is performed, so nothing can overflow.
 */
int clamp_int(int x, int lo, int hi, int *out);

#endif
