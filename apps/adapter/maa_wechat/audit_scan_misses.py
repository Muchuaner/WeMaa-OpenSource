"""Read-only diagnosis of old OCR list filtering from Maa action evidence."""
import json
from pathlib import Path
from flows import lines,box

root=Path(__file__).resolve().parent
excluded=set()
reads=0
for line in (root/'data'/'actions.jsonl').open(encoding='utf-8'):
    result=json.loads(line)
    if result.get('node')!='Read': continue
    for node in (result.get('detail') or {}).get('nodes',[]):
        items=(node.get('recognition') or {}).get('all_results') or []
        if not items or max(box(i)[0] for i in items)>500 or max(box(i)[1] for i in items)<800: continue
        reads+=1
        for row in lines(items):
            xs=[box(i)[0] for i in row['items']]
            if min(xs)<120 and max(xs)>=130 and row['y']>300:
                excluded.add(row['text'])
print(json.dumps({'list_ocr_reads':reads,'distinct_rows_with_photo_text_contamination':len(excluded)}))
