# ipipe.sh <outdir> <src.c> <harness.c> : integration-style argv order (editable, harness, models)
O=$1; SRC=$2; H=$3; mkdir -p $O
goto-cc -D__CPROVER__ -m64 -std=gnu11 -I$INC -o $O/p.goto $SRC $H im_models.c 2> $O/cc.err || { echo "goto-cc failed"; head -3 $O/cc.err; exit 1; }
goto-instrument --replace-calls memcpy:leanc_inductive_memcpy --replace-calls memmove:leanc_inductive_memmove --replace-calls memset:leanc_inductive_memset --drop-unused-functions $O/p.goto $O/r.goto > $O/rc.log 2>&1 || { echo "replace failed"; exit 1; }
goto-instrument --dfcc main --apply-loop-contracts $O/r.goto $O/d.goto > $O/gi.log 2>&1 || { echo "dfcc failed"; exit 1; }
S=$(date +%s); timeout 300 cbmc $O/d.goto --no-library --bounds-check --pointer-check --div-by-zero-check --signed-overflow-check --pointer-overflow-check --undefined-shift-check --unwind 8 --unwinding-assertions --json-ui > $O/r.json 2> $O/r.err; echo "cbmc exit $? in $(( $(date +%s) - S )) s"
python3 show.py $O/r.json fail
