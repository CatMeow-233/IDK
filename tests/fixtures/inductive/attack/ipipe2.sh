# ipipe2.sh <outdir> <src.c> <harness.c> <verify|vacuity>
O=$1; SRC=$2; H=$3; M=$4; mkdir -p $O
VD=""; CK="--bounds-check --pointer-check --div-by-zero-check --signed-overflow-check --pointer-overflow-check --undefined-shift-check --memory-leak-check"
[ "$M" = vacuity ] && { VD="-DLEANC_VACUITY_CHECK"; CK=""; }
goto-cc -D__CPROVER__ -m64 -std=gnu11 -D__LP64__ -D__x86_64 -D__x86_64__ -D_LP64 -D__amd64__ -D__amd64 $VD -I$INC -o $O/p.goto $SRC $H im_models.c 2> $O/cc.err || { echo "goto-cc failed"; exit 1; }
goto-instrument --replace-calls memcpy:leanc_inductive_memcpy --replace-calls memmove:leanc_inductive_memmove --replace-calls memset:leanc_inductive_memset --drop-unused-functions $O/p.goto $O/r.goto > $O/rc.log 2>&1 || { echo "replace failed"; exit 1; }
LOOPS='import json,sys; d=json.load(open(sys.argv[1])); print(sorted((l["name"], l["sourceLocation"].get("file"), l["sourceLocation"].get("line")) for m in d if "loops" in m for l in m["loops"] if not l["sourceLocation"].get("file","").startswith("<builtin")))'
goto-instrument --show-loops --json-ui $O/r.goto > $O/loops.before.json 2>/dev/null; echo "inventory: $(python3 -c "$LOOPS" $O/loops.before.json)"
goto-instrument --dfcc main --apply-loop-contracts $O/r.goto $O/d.goto > $O/gi.log 2>&1 || { echo "dfcc failed"; exit 1; }
goto-instrument --show-loops --json-ui $O/d.goto > $O/loops.after.json 2>/dev/null; echo "residual: $(python3 -c "$LOOPS" $O/loops.after.json)"
goto-instrument --list-symbols $O/r.goto 2>/dev/null | grep -o "^leanc_inductive_memcpy[^ ]*" | sort -u | tr '\n' ' '; echo
S=$(date +%s); timeout 300 cbmc $O/d.goto --no-library $CK --unwind 8 --unwindset __CPROVER_contracts_write_set_check_assigns_clause_inclusion.0:18 --unwinding-assertions --json-ui > $O/r.json 2> $O/r.err; echo "cbmc exit $? in $(( $(date +%s) - S )) s"
python3 - $O/r.json <<'PY'
import json,sys,collections,re
d=json.load(open(sys.argv[1])); res=[r for m in d if 'result' in m for r in m['result']]
print('properties', len(res), 'non-SUCCESS', sum(r['status']!='SUCCESS' for r in res))
cls=collections.defaultdict(collections.Counter)
for r in res:
    m=re.search(r'for loop (\S+)$', r['description'])
    if m: cls[m.group(1)][(r['property'].split('.')[-2], r['status'])]+=1
for k,v in sorted(cls.items()): print(' ', k, dict(v))
print('  im_models.c properties:', sum(1 for r in res if r.get('sourceLocation',{}).get('file','')=='im_models.c'))
for r in res:
    if r['status']!='SUCCESS': print('  ', r['status'], r['property'], '|', r['description'][:90], '|', r.get('sourceLocation',{}).get('file'), r.get('sourceLocation',{}).get('line'))
for m in d:
    if m.get('messageType')=='ERROR': print('  ERROR', m['messageText'][:150])
PY
