import json
from pathlib import Path
root=Path(__file__).resolve().parent
rows=[]
for line in (root/'data'/'actions.jsonl').open(encoding='utf-8'):
    result=json.loads(line)
    if result['node'] in ('ContactsScrollUp','ContactsScrollDown','ReadContactProfile','SelectRecentRow'):
        rows.append({'node':result['node'],'elapsed_ms':result['elapsed_ms']})
print(json.dumps(rows[-20:]))
