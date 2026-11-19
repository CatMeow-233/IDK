/* Interface of the small C functions used by the CBMC backend tests. */
#ifndef CBMC_FX_H
#define CBMC_FX_H

#include <stddef.h>
#include <stdint.h>

#define ARR_OK 0
#define ARR_ERR_NULL (-1)
#define ARR_ERR_RANGE (-2)

/* Read arr[idx] into *out; ARR_ERR_RANGE if idx >= len, ARR_ERR_NULL on NULL. */
int arr_get(const int *arr, size_t len, size_t idx, int *out);

/* total / count (integer average). */
int avg(int total, int count);

/* a + b */
int add(int a, int b);

/* *p */
int deref(const int *p);

/* Number of zero elements in a[0..n). */
size_t count_zeros(const int *a, size_t n);

/* (a * b) / b, for b != 0. */
uint64_t mul_div(uint64_t a, uint64_t b);

#endif
