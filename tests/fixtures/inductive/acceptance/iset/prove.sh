#!/bin/bash
# usage: prove.sh <variant> <entry> [vac]   (per-entry link, DFCC loop contracts, cbmc)
V=$1; E=$2; H=harness; [ "${3:-}" = vac ] && H=harness.vac
B=$V/build/$E.$H
goto-cc -m64 -std=gnu11 $V/build/iset.i $V/build/$H.i ${MODELS:-} --function $E -o $B.goto 2> $B.cc.err || { echo "GOTO-CC FAILED"; cat $B.cc.err; exit 0; }
goto-instrument --dfcc $E --apply-loop-contracts $B.goto $B.dfcc.goto > $B.gi.log 2>&1; rc=$?
[ $rc -ne 0 ] && { echo "GOTO-INSTRUMENT rc=$rc"; grep -v "already has an initial" $B.gi.log | tail -4; exit 0; }
grep -i "no body" $B.gi.log | grep -v "__CPROVER_"
( time timeout ${TMO:-120} cbmc $B.dfcc.goto --bounds-check --pointer-check --div-by-zero-check --signed-overflow-check --pointer-overflow-check --undefined-shift-check --unsigned-overflow-check --memory-leak-check --unwind ${UNW:-1} --unwinding-assertions --no-library --json-ui > $B.json 2> $B.err ) 2> $B.time
echo "cbmc rc=$? $(grep real $B.time)"
python3 /tmp/claude-0/-home-user-IDK/e45e3e13-7c56-5b3f-aae6-8a7ea9b421c4/scratchpad/design/acceptance/proto/tools/summ.py $B.json
