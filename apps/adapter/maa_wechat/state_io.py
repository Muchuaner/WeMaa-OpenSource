"""Bounded Windows share-lock recovery; never replaces a GUI send journal."""
import json,os,tempfile,time
from pathlib import Path

def atomic_json(path,value,*,best_effort=False,attempts=8):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix=path.name+'.',suffix='.tmp',dir=path.parent)
    pending=Path(name)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            json.dump(value,stream,ensure_ascii=False,indent=2)
        for attempt in range(attempts):
            try:
                os.replace(pending,path)
                return True
            except PermissionError:
                if attempt+1==attempts:
                    if best_effort:return False
                    raise
                time.sleep(.025*(attempt+1))
    finally:
        try:pending.unlink(missing_ok=True)
        except OSError:pass
