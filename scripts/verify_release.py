"""Compare every packaged WebUI file with the locally built release manifest."""
from pathlib import Path

import argparse
import hashlib
import json
import re
import subprocess


PROBE = """from pathlib import Path
import hashlib,json
root=Path('/opt/wemaa/webui');h=hashlib.sha256();files=sorted(p for p in root.rglob('*') if p.is_file())
for p in files:h.update((p.relative_to(root).as_posix()+'\\0'+hashlib.sha256(p.read_bytes()).hexdigest()+'\\n').encode())
print(json.dumps({'files':len(files),'sha256':h.hexdigest()}))
"""


def write_manifest(directory: Path, destination: Path) -> dict:
    if not (directory / 'index.html').is_file():
        raise ValueError('Build the complete WebUI before creating a release manifest')
    files = sorted(path for path in directory.rglob('*') if path.is_file())
    digest = hashlib.sha256()
    for path in files:
        digest.update((path.relative_to(directory).as_posix() + '\0'
                       + hashlib.sha256(path.read_bytes()).hexdigest() + '\n').encode())
    manifest = {'files': len(files), 'sha256': digest.hexdigest()}
    destination.write_text(json.dumps(manifest, indent=2) + '\n')
    destination.chmod(0o600)
    return manifest


def verify_image(image: str, manifest: Path) -> dict:
    expected = json.loads(manifest.read_text())
    if (set(expected) != {'files', 'sha256'} or type(expected['files']) is not int
            or expected['files'] <= 0 or not re.fullmatch('[a-f0-9]{64}', expected['sha256'])):
        raise ValueError('A complete local WebUI release manifest is required')
    actual = json.loads(subprocess.check_output(
        ['docker', 'run', '--rm', '--network', 'none', '-i', '--entrypoint', 'python', image, '-'],
        input=PROBE.encode(), timeout=60))
    if actual != expected:
        raise RuntimeError('Packaged WebUI differs from local build; runtime update refused')
    return actual


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--image')
    mode.add_argument('--dist', type=Path)
    parser.add_argument('--manifest', required=True, type=Path)
    args = parser.parse_args()
    if args.dist:
        print(json.dumps(write_manifest(args.dist, args.manifest)))
    else:
        print(json.dumps({'verified': True, **verify_image(args.image, args.manifest)}))
