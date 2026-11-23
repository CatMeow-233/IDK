# usage: pipeline.sh <builddir> <entry> <mode> <editable.c> <contracts.json> <harness.c> <unwind>
B=$1; entry=$2; mode=$3; SRC=$4; CON=$5; HAR=$6; UNW=$7
mkdir -p $B
VD=""; [ $mode = vacuity ] && VD="-DLEANC_VACUITY_CHECK"
cbmc --preprocess --c11 $INC $SRC 2> $B/pp.src.err | tail -n +2 > $B/src.i
cbmc --preprocess --c11 $INC $VD $HAR 2> $B/pp.har.err | tail -n +2 > $B/harness.i
cbmc --preprocess --c11 $INC -DLEANC_INDUCTIVE_ENTRY=$entry models.c 2> $B/pp.mod.err | tail -n +2 > $B/models.i
python3 inject.py $B/src.i $B/src.ann.i $CON x || exit 3
goto-cc -std=gnu11 --function leanc_inductive_entry -o $B/prog.goto $B/src.ann.i $B/harness.i $B/models.i 2> $B/cc.err; echo "goto-cc=$? $(head -c 300 $B/cc.err)"
goto-instrument --show-loops --json-ui $B/prog.goto > $B/loops.before.json 2>/dev/null
goto-instrument --dfcc leanc_inductive_entry --apply-loop-contracts $B/prog.goto $B/prog.dfcc.goto > $B/gi.log 2>&1; echo "gi=$?"
goto-instrument --show-loops --json-ui $B/prog.dfcc.goto > $B/loops.after.json 2>/dev/null
CK="$CHK"; [ $mode = vacuity ] && CK=""
/usr/bin/env bash -c "time cbmc $B/prog.dfcc.goto $CK --no-library --unwind $UNW --unwinding-assertions --json-ui > $B/cbmc.json 2> $B/cbmc.err" 2>&1 | grep real
python3 show.py $B/cbmc.json "leanc_" | grep -v "is assignable\|is valid" | cut -c1-210
