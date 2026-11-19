#ifndef MIDPOINT_H
#define MIDPOINT_H

/*
 * Midpoint of two ints: the exact mathematical value (a + b) / 2 rounded
 * toward zero (i.e. what C's / would give if a + b could not overflow).
 * Defined for ALL int inputs; the result always lies between a and b.
 */
int midpoint(int a, int b);

#endif
