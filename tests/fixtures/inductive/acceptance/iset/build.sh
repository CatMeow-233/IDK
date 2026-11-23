#!/bin/bash
# usage: build.sh <variant-dir> ; produces build/prog.goto build/prog.lc.goto build/vac.lc.goto
set -u
V=$1; INC=/home/user/IDK/leanc/data/include; T=$(dirname $0)/../tools
mkdir -p $V/build/annotated/src
python3 $T/annotate.py $V/src/iset.c contracts.json $V/build/annotated/src/iset.c src/iset.c > $V/build/loops.json
pp() { # in out extra...
  local in=$1 out=$2; shift 2
  cbmc --preprocess "$in" -I$V/include -I$INC --c11 "$@" > $out.raw 2> $out.err
  [ -s $out.err ] && { echo "PREPROCESS DIAGNOSTICS for $in"; cat $out.err; return 1; }
  head -1 $out.raw | grep -q '^CBMC version' || { echo "no banner"; return 1; }
  tail -n +2 $out.raw > $out
}
pp $V/build/annotated/src/iset.c $V/build/iset.i || exit 1
pp iset_inductive.c $V/build/harness.i || exit 1
pp iset_inductive.c $V/build/harness.vac.i -DLEANC_VACUITY_CHECK || exit 1
goto-cc -m64 -std=gnu11 $V/build/iset.i $V/build/harness.i -o $V/build/prog.goto 2> $V/build/gotocc.err || { echo GOTO-CC FAILED; cat $V/build/gotocc.err; exit 1; }
goto-cc -m64 -std=gnu11 $V/build/iset.i $V/build/harness.vac.i -o $V/build/vac.goto 2>> $V/build/gotocc.err || { echo GOTO-CC FAILED; exit 1; }
for b in prog vac; do
  goto-instrument --apply-loop-contracts $V/build/$b.goto $V/build/$b.lc.goto > $V/build/$b.gi.log 2>&1 || { echo GOTO-INSTRUMENT FAILED rc=$?; tail -5 $V/build/$b.gi.log; exit 1; }
done
echo built $V
