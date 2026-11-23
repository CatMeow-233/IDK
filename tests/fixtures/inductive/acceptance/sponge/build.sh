#!/bin/bash
# usage: build.sh <variant-dir> ; produces build/prog.goto build/prog.lc.goto build/vac.lc.goto
set -u
V=$1; INC=/home/user/IDK/leanc/data/include; T=$(dirname $0)/../tools
mkdir -p $V/build/annotated/src
python3 $T/annotate.py $V/src/sponge.c contracts.json $V/build/annotated/src/sponge.c src/sponge.c > $V/build/loops.json
pp() { # in out extra...
  local in=$1 out=$2; shift 2
  cbmc --preprocess "$in" -I$V/include -I$INC --c11 "$@" > $out.raw 2> $out.err
  [ -s $out.err ] && { echo "PREPROCESS DIAGNOSTICS for $in"; cat $out.err; return 1; }
  head -1 $out.raw | grep -q '^CBMC version' || { echo "no banner"; return 1; }
  tail -n +2 $out.raw > $out
}
pp $V/build/annotated/src/sponge.c $V/build/sponge.i || exit 1
pp sponge_inductive.c $V/build/harness.i || exit 1
pp sponge_inductive.c $V/build/harness.vac.i -DLEANC_VACUITY_CHECK || exit 1
echo built $V
