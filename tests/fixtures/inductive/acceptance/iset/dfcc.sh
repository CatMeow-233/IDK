#!/bin/bash
# usage: dfcc.sh <variant> <entry> [bin=prog]
V=$1; E=$2; B=${3:-prog}
goto-instrument --dfcc $E --apply-loop-contracts $V/build/$B.goto $V/build/$B.$E.dfcc.goto > $V/build/$B.$E.gi.log 2>&1; rc=$?
[ $rc -ne 0 ] && { echo "GOTO-INSTRUMENT rc=$rc"; tail -4 $V/build/$B.$E.gi.log; exit 0; }
grep -i "no body" $V/build/$B.$E.gi.log
( time timeout ${TMO:-120} cbmc $V/build/$B.$E.dfcc.goto --bounds-check --pointer-check --div-by-zero-check --signed-overflow-check --pointer-overflow-check --undefined-shift-check --unsigned-overflow-check --memory-leak-check --unwind ${UNW:-1} --unwinding-assertions --json-ui > $V/build/$B.$E.json 2> $V/build/$B.$E.err ) 2> $V/build/$B.$E.time
echo "cbmc rc=$? $(grep real $V/build/$B.$E.time)"
python3 - $V/build/$B.$E.json <<'PY'
import json,sys
try:
    data=json.load(open(sys.argv[1]))
except Exception as e:
    print("unparsable", e); sys.exit()
for m in data:
    if 'result' in m:
        res=m['result']; bad=[r for r in res if r['status']!='SUCCESS']
        print(len(res),'properties;',len(bad),'not SUCCESS')
        for r in bad[:12]:
            loc=r.get('sourceLocation',{})
            print('  ',r['status'],r['property'],'|',r['description'][:110],'|',loc.get('file'),loc.get('line'),loc.get('function'))
    if 'cProverStatus' in m: print('cProverStatus',m['cProverStatus'])
    if m.get('messageType')=='ERROR': print('ERROR',m.get('messageText'))
PY
