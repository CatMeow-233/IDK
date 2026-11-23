#!/bin/bash
# usage: run.sh <variant> <entry> [bin=prog]
V=$1; E=$2; B=${3:-prog}
( time timeout ${TMO:-120} cbmc $V/build/$B.lc.goto --function $E --bounds-check --pointer-check --div-by-zero-check --signed-overflow-check --pointer-overflow-check --undefined-shift-check --unsigned-overflow-check --memory-leak-check --unwind 1 --unwinding-assertions --drop-unused-functions --json-ui > $V/build/$B.$E.json 2> $V/build/$B.$E.err ) 2> $V/build/$B.$E.time
echo "rc=$? $(grep real $V/build/$B.$E.time)"
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
            print('  ',r['status'],r['property'],'|',r['description'],'|',loc.get('file'),loc.get('line'),loc.get('function'))
    if 'cProverStatus' in m: print('cProverStatus',m['cProverStatus'])
    if m.get('messageType')=='ERROR': print('ERROR',m.get('messageText'))
PY
