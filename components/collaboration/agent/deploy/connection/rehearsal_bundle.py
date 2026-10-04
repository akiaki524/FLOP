"""Build a secret-free rehearsal payload from a committed checkout and pinned runtime.

Usage: python3 -B deploy/connection/rehearsal_bundle.py /path/to/node22 /tmp/payload.tar
"""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile


def main():
    repo = Path(__file__).resolve().parents[2]
    node = Path(sys.argv[1]).resolve(strict=True)
    out = Path(sys.argv[2])
    if subprocess.check_output(['git', 'status', '--porcelain'], cwd=repo).strip():
        raise RuntimeError('COMMITTED_CLEAN_CHECKOUT_REQUIRED')
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()
    archive = subprocess.check_output(['git', 'archive', head], cwd=repo)
    pin = json.loads((repo / 'src/collaboration_agent/tclk_pin.json').read_text())
    if not subprocess.check_output([str(node), '--version']).startswith(b'v22.'):
        raise RuntimeError('NODE22_REQUIRED')
    extras = [Path('.local/batch17a/candidate.json')]
    for name, digest in pin['files'].items():
        path = Path('.local/batch16/official-runtime') / name
        if Path(name).is_absolute() or '..' in Path(name).parts or (repo / path).is_symlink() \
                or hashlib.sha256((repo / path).read_bytes()).hexdigest() != digest:
            raise RuntimeError('RUNTIME_PIN_MISMATCH')
        extras.append(path)
    with tarfile.open(fileobj=io.BytesIO(archive)) as source, tarfile.open(out, 'x') as target:
        for info in source:
            if not (info.isfile() or info.isdir()):
                raise RuntimeError('ARCHIVE_TYPE_REFUSED')
            target.addfile(info, source.extractfile(info) if info.isfile() else None)
        for path in extras:
            target.add(repo / path, arcname=str(path), recursive=False)
        target.add(node, arcname='node22', recursive=False)
        data = (json.dumps({'head': head, 'node_sha256': hashlib.sha256(node.read_bytes()).hexdigest()}) + '\n').encode()
        info = tarfile.TarInfo('rehearsal-source.json')
        info.size, info.mode = len(data), 0o644
        target.addfile(info, io.BytesIO(data))
    print(json.dumps({'head': head, 'payload_sha256': hashlib.sha256(out.read_bytes()).hexdigest()}))

if __name__ == '__main__':
    main()
