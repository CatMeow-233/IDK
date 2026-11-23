#!/bin/bash
# usage: lc.sh file.c [more.c...] ; compile, apply loop contracts, run cbmc, summarise
out=$(basename $1 .c)
goto-cc -m64 -std=gnu11 -D__CPROVER__ "$@" -o $out.goto 2> $out.cc.err; rc=$?
if [ $rc -ne 0 ]; then echo "[$out] GOTO-CC rc=$rc: $(grep -v '^$' $out.cc.err | head -3 | tr '\n' ' ')"; exit 0; fi
goto-instrument ${GI:---apply-loop-contracts} $out.goto $out.lc.goto > $out.gi.log 2>&1; rc=$?
if [ $rc -ne 0 ]; then echo "[$out] GOTO-INSTRUMENT rc=$rc: $(tail -3 $out.gi.log | tr '\n' ' ')"; exit 0; fi
grep -i "no body\|ignored\|warning" $out.gi.log | sed "s/^/[$out] gi: /"
timeout 60 cbmc $out.lc.goto --bounds-check --pointer-check --signed-overflow-check --unsigned-overflow-check --pointer-overflow-check --unwind ${UNW:-1} --unwinding-assertions ${CBMC_EXTRA:-} --json-ui > $out.json 2> $out.err; rc=$?
python3 - $out.json $out $rc <<'PY'
import json,sys
name=sys.argv[2]
try: data=json.load(open(sys.argv[1]))
except Exception as e: print(f"[{name}] cbmc rc={sys.argv[3]} unparsable"); sys.exit()
for m in data:
    if 'result' in m:
        bad=[r for r in m['result'] if r['status']!='SUCCESS']
        print(f"[{name}] cbmc rc={sys.argv[3]}: {len(m['result'])} props, {len(bad)} failed" + "".join(f"\n      {r['status']} {r['property']}: {r['description']} @{r.get('sourceLocation',{}).get('line')}" for r in bad[:6]))
    if m.get('messageType')=='ERROR': print(f"[{name}] ERROR {m.get('messageText')[:200]}")
PY
