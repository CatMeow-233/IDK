/*
 * Trusted differential-test driver (human-reviewed): reads the vectors that
 * lean/Vectors.lean computes from the Lean model on stdin, one per line
 *     <x> <lo> <hi> ok <r>     (model: clamp x lo hi = some r)
 *     <x> <lo> <hi> err 0      (model: clamp x lo hi = none)
 * and checks clamp_int against each.  Protocol (see leanc/backends/lean.py):
 * every disagreement prints "LEANC-VECTOR-MISMATCH key=value ..." and the
 * driver exits 1; on success it prints "LEANC-VECTORS-CHECKED <n>".
 */
#include <stdio.h>
#include <string.h>

#include "clamp.h"

#define SENTINEL 0x5a5a5a5a

int main(void)
{
    char line[256];
    long checked = 0;
    long bad = 0;
    while (fgets(line, sizeof line, stdin) != NULL) {
        if (line[0] == '#' || line[0] == '\n') {
            continue;
        }
        long long x, lo, hi, want;
        char kind[8];
        if (sscanf(line, "%lld %lld %lld %7s %lld", &x, &lo, &hi, kind, &want) != 5) {
            fprintf(stderr, "malformed vector: %s", line);
            return 2;
        }
        int out = SENTINEL;
        int rc = clamp_int((int)x, (int)lo, (int)hi, &out);
        int ok;
        if (strcmp(kind, "ok") == 0) {
            ok = rc == CLAMP_OK && (long long)out == want;
        } else {
            ok = rc == CLAMP_ERR_RANGE && out == SENTINEL;
        }
        checked++;
        if (!ok) {
            bad++;
            if (bad <= 10) {
                printf("LEANC-VECTOR-MISMATCH x=%lld lo=%lld hi=%lld expected=%s:%lld got_rc=%d got_out=%d\n",
                       x, lo, hi, kind, want, rc, out);
            }
        }
    }
    if (bad != 0) {
        printf("%ld of %ld vector(s) disagree with the Lean model\n", bad, checked);
        return 1;
    }
    printf("LEANC-VECTORS-CHECKED %ld\n", checked);
    return 0;
}
