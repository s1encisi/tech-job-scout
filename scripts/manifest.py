#!/usr/bin/env python3
"""Build or verify SHA-256 checksums for tracked distributable source files."""
import argparse
import hashlib
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def render():
    result = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, check=True, capture_output=True)
    names = sorted(n for n in result.stdout.decode('utf-8').split('\0')
                   if n and n != 'MANIFEST.sha256')
    if not names:
        raise RuntimeError('No tracked files; stage the source files first.')
    lines = []
    for name in names:
        data = (ROOT / name).read_bytes()
        # Repository text is LF; normalize Windows checkout line endings.
        try:
            data.decode('utf-8')
        except UnicodeDecodeError:
            pass
        else:
            data = data.replace(b'\r\n', b'\n')
        lines.append(hashlib.sha256(data).hexdigest() + '  ' + name)
    return '\n'.join(lines) + '\n'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['build', 'check'])
    args = parser.parse_args()
    content = render()
    path = ROOT / 'MANIFEST.sha256'
    if args.mode == 'build':
        path.write_text(content, encoding='utf-8', newline='\n')
    elif path.read_text(encoding='utf-8') != content:
        raise SystemExit('Manifest mismatch; review changes before rebuilding.')
    print('Manifest ' + args.mode + ' OK')
