# usage: run.sh file.c entry [extra cbmc flags]  -> DFCC pipeline in-source contracts
f=$1; e=$2; shift 2; b=${f%.c}
goto-cc --function $e -o $b.goto $f 2> $b.cc.err || { echo "goto-cc exit $?: $(head -c 300 $b.cc.err)"; exit; }
goto-instrument --dfcc $e --apply-loop-contracts $b.goto $b.d.goto > $b.gi.log 2>&1 || { echo "gi exit $?"; tail -3 $b.gi.log; exit; }
cbmc $b.d.goto --no-library --bounds-check --pointer-check --signed-overflow-check --unwind 4 --unwinding-assertions "$@" --json-ui > $b.json 2> $b.err; echo "cbmc exit $?"
python3 show.py $b.json fail
