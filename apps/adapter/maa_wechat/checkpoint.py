"""Consistent SQLite backup, including committed WAL transactions."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import zipfile
from PIL import Image

root=Path(__file__).resolve().parent
parser=argparse.ArgumentParser()
parser.add_argument('--id',required=True)
parser.add_argument('--bundle',action='store_true')
parser.add_argument('--since',help='Prior checkpoint filename: bundle only newer data evidence; apply over that full recovery bundle')
args=parser.parse_args()
since=None
if args.since:
    previous=(root/'data'/args.since).resolve()
    if previous.parent!=(root/'data').resolve() or not previous.name.startswith('checkpoint-') or previous.suffix!='.sqlite3': raise ValueError('INVALID_PRIOR_CHECKPOINT')
    since=previous.stat().st_mtime
if not args.id.replace('-','').replace('_','').isalnum(): raise ValueError('INVALID_ID')
target=root/'data'/('checkpoint-'+args.id+'.sqlite3')
if target.exists(): raise FileExistsError(target)
source=sqlite3.connect(f'file:{(root/"data"/"wechat.sqlite3").as_posix()}?mode=ro',uri=True)
destination=sqlite3.connect(target)
source.backup(destination)
integrity=destination.execute('PRAGMA integrity_check').fetchone()[0]
destination.close()
source.close()
if integrity!='ok': raise RuntimeError(integrity)
report={'path':str(target),'integrity':integrity,'sha256':hashlib.sha256(target.read_bytes()).hexdigest()}
if args.bundle:
    archive=root/'data'/('recovery-'+args.id+'.zip')
    with zipfile.ZipFile(archive,'x',compression=zipfile.ZIP_STORED) as bundle:
        bundle.write(target,'data/wechat.sqlite3')
        for path in root.iterdir():
            if path.is_file() and path.suffix in ('.py','.md','.txt','.json'): bundle.write(path,path.name)
        for path in (root/'resource').rglob('*'):
            if path.is_file(): bundle.write(path,path.relative_to(root).as_posix())
        for path in (root/'tools').glob('*.ps1'):
            bundle.write(path,path.relative_to(root).as_posix())
        for path in (root/'data').rglob('*'):
            relative=path.relative_to(root)
            owned=path.parent==root/'data' or 'avatars' in relative.parts
            if owned and path.is_file() and (path.suffix in ('.png','.json','.jsonl')) and 'offline' not in relative.parts and (since is None or path.stat().st_mtime>=since):
                # Freeze and validate each live evidence file before archiving;
                # the desktop worker may overwrite its current viewport PNG.
                for attempt in range(5):
                    raw=path.read_bytes()
                    try:
                        if path.suffix=='.png':
                            with Image.open(io.BytesIO(raw)) as picture: picture.verify()
                        elif path.suffix=='.json': json.loads(raw.decode('utf-8-sig'))
                        else:
                            # A concurrently appended final log line is not yet
                            # committed evidence. Preserve complete lines only.
                            raw=raw[:raw.rfind(b'\n')+1]
                        break
                    except (OSError,ValueError,SyntaxError):
                        if attempt==4: raise RuntimeError('UNSTABLE_EVIDENCE:'+relative.as_posix())
                bundle.writestr(relative.as_posix(),raw)
    with zipfile.ZipFile(archive) as bundle:
        if bundle.testzip() is not None: raise RuntimeError('BUNDLE_CORRUPT')
    report.update(bundle=str(archive),bundle_bytes=archive.stat().st_size,bundle_sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
    if args.since: report['apply_over_full_checkpoint']=args.since
print(json.dumps(report))
