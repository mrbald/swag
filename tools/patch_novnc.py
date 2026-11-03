#!/usr/bin/env python3
"""Apply SWAG's bounded clipboard-availability hook to pinned noVNC 1.7.0."""
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

UPSTREAM_SHA256 = 'c46dfefbf869eda0c6d06c5dd86c42e96fc8a77c125e91599a2cdf3841261564'
PATCHED_SHA256 = '5d002c0b4f0f572e97bc1b9b88f7963ed5b1a40bc3b9ad650c5d3eb8a7aadfca'
PATCH_FILE = Path(__file__).resolve().parents[1] / 'patches/novnc/0001-clipboard-unavailable.patch'


def patch(path):
    path = Path(path).resolve()
    original = path.read_bytes()
    if hashlib.sha256(original).hexdigest() != UPSTREAM_SHA256:
        raise ValueError('Unexpected noVNC rfb.js; review the patch for this version before building')
    # Work on a temporary copy so a failed patch or output check leaves the
    # installed viewer untouched. The unified diff is the only patch source.
    with tempfile.TemporaryDirectory(prefix='swag-novnc-') as directory:
        root = Path(directory)
        (root / 'core').mkdir()
        target = root / 'core/rfb.js'
        target.write_bytes(original)
        subprocess.run([
            'patch', '--batch', '--forward', '--fuzz=0', '-p1',
            '-d', str(root), '-i', str(PATCH_FILE),
        ], check=True, stdout=sys.stderr)
        patched = target.read_bytes()
        if hashlib.sha256(patched).hexdigest() != PATCHED_SHA256:
            raise ValueError('Unexpected patched noVNC rfb.js; review the patch before building')
    # Replace on the same filesystem only after writing all bytes successfully.
    with tempfile.TemporaryDirectory(prefix='.swag-novnc-', dir=path.parent) as directory:
        staged = Path(directory) / path.name
        staged.write_bytes(patched)
        shutil.copymode(path, staged)
        staged.replace(path)
    return hashlib.sha256(patched).hexdigest()


if __name__ == '__main__':
    print(patch(sys.argv[1]))
