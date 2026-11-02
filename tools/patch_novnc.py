#!/usr/bin/env python3
"""Apply SWAG's bounded clipboard-availability hook to pinned noVNC 1.7.0."""
import hashlib
from pathlib import Path
import sys

UPSTREAM_SHA256 = 'c46dfefbf869eda0c6d06c5dd86c42e96fc8a77c125e91599a2cdf3841261564'
BEFORE = '''            } else if (actions === extendedClipboardActionNotify) {
                if (this._viewOnly) {
                    return true;
                }

'''
AFTER = BEFORE + '''                // SWAG: surface standard notification that no text is available.
                // Do not leave a previous remote or local copy looking current.
                if (!(formats & extendedClipboardFormatText)) {
                    this._clipboardText = null;
                    this.dispatchEvent(new CustomEvent("clipboardunavailable"));
                }

'''


def patch(path):
    path = Path(path)
    original = path.read_bytes()
    if hashlib.sha256(original).hexdigest() != UPSTREAM_SHA256:
        raise ValueError('Unexpected noVNC rfb.js; review the patch for this version before building')
    text = original.decode('utf-8')
    if text.count(BEFORE) != 1:
        raise ValueError('Expected one clipboard Notify handler')
    patched = text.replace(BEFORE, AFTER).encode('utf-8')
    path.write_bytes(patched)
    return hashlib.sha256(patched).hexdigest()


if __name__ == '__main__':
    print(patch(sys.argv[1]))
