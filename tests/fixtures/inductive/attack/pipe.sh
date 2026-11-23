# pipe.sh <outdir> <entry> <src.c> <harness.c> [vacuity]  : soundness-style entry wrapper, DFCC
O=$1; E=$2; SRC=$3; H=$4; V=$5; mkdir -p $O
VD=""; CK="--bounds-check --pointer-check --div-by-zero-check --signed-overflow-check --pointer-overflow-check --undefined-shift-check"
[ "$V" = vacuity ] && { VD="-DLEANC_VACUITY_CHECK"; CK=""; }
sed "s/^void entry(void);/void $E(void);/; s/    entry();/    $E();/" sm_models.c > $O/models.c
goto-cc -D__CPROVER__ $VD -std=gnu11 -I$INC --function leanc_inductive_entry -o $O/p.goto $SRC $H $O/models.c 2> $O/cc.err || { echo "goto-cc exit: $(head -c 300 $O/cc.err)"; exit 1; }
goto-instrument --dfcc leanc_inductive_entry --apply-loop-contracts $O/p.goto $O/d.goto > $O/gi.log 2>&1 || { echo "gi exit $?"; tail -2 $O/gi.log; exit 1; }
goto-instrument --show-loops --json-ui $O/d.goto 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); [print('residual loop', l['name'], l['sourceLocation'].get('file'), l['sourceLocation'].get('line')) for m in d if 'loops' in m for l in m['loops'] if not l['sourceLocation'].get('file','').startswith('<builtin')]"
S=$(date +%s.%N); timeout 300 cbmc $O/d.goto --no-library $CK --unwind 3 --unwinding-assertions --json-ui > $O/r.json 2> $O/r.err; echo "cbmc exit $? in $(echo "$(date +%s.%N) - $S" | bc) s"
python3 show.py $O/r.json fail | grep -v "leanc_canary"
