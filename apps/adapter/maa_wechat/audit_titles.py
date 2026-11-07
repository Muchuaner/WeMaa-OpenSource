import json
from pathlib import Path
rows=[]
for line in (Path(__file__).resolve().parent/'data'/'actions.jsonl').open(encoding='utf-8'):
    entry=json.loads(line)
    if entry.get('node')=='ReadChatTitle':
        hits=[h for node in (entry.get('detail') or {}).get('nodes',[]) for h in (node.get('recognition') or {}).get('all_results',[])]
        rows.append(hits)
print(json.dumps(rows[-30:],ensure_ascii=True))
