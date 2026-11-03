# noVNC source and patch

We use the upstream **noVNC 1.7.0 release source**, including its readable,
non-minified JavaScript ES modules. There is no generated bundle or npm build.
The full upstream tree is downloaded during image/viewer preparation; this
directory keeps our small change reviewable without maintaining a vendored fork.
Keep the upstream license files with any redistributed viewer tree.

[`0001-clipboard-unavailable.patch`](0001-clipboard-unavailable.patch) handles an
Extended Clipboard Notify with no Text format: clear cached clipboard text and
emit `clipboardunavailable`. The browser page must handle that event to show
failure feedback; our [`clipboard.js`](../../web/clipboard.js) does so. The patch
does not change compression, rendering or transport performance.

The [viewer preparation recipe](../../docs/integration/vncws.md#prepare-the-local-viewer)
downloads and verifies the release. From the repository root, the normal command is:

```sh
python3.12 tools/patch_novnc.py artifacts/client-viewer/core/rfb.js
```

This wrapper requires `patch` on PATH. It verifies the original file, applies the
tracked diff with `patch -p1 --batch --forward --fuzz=0` to a temporary copy, and
verifies the output before atomically replacing the installed file, preserving
its permission mode. The target directory must be writable. Drift, double application,
missing tools and patch errors fail without changing the target. Container builds
use this same wrapper and diff.

To apply it directly with conventional tools instead, start with a **freshly
extracted** upstream tree and run from this repository root (Linux, with `patch`
and `sha256sum` installed):

```sh
(
  set -eu
  echo 'c46dfefbf869eda0c6d06c5dd86c42e96fc8a77c125e91599a2cdf3841261564  artifacts/client-viewer/core/rfb.js' | sha256sum --check -
  patch --batch --forward --fuzz=0 -p1 -d artifacts/client-viewer < patches/novnc/0001-clipboard-unavailable.patch
  echo '5d002c0b4f0f572e97bc1b9b88f7963ed5b1a40bc3b9ad650c5d3eb8a7aadfca  artifacts/client-viewer/core/rfb.js' | sha256sum --check -
)
```

The direct command edits the extracted tree in place. Discard that tree if patch
application or the final checksum fails. The wrapper is preferable for automation
because it checks the result before modifying the target.

Pinned SHA-256 values:

| Input/output | SHA-256 |
| --- | --- |
| `v1.7.0` release tarball | `b1003a11b6e6e8d8f7f5e5586daae7f8ca651d8aee0aa155ff9ac841c48f52c6` |
| Original `core/rfb.js` | `c46dfefbf869eda0c6d06c5dd86c42e96fc8a77c125e91599a2cdf3841261564` |
| Patched `core/rfb.js` | `5d002c0b4f0f572e97bc1b9b88f7963ed5b1a40bc3b9ad650c5d3eb8a7aadfca` |

For an upstream upgrade, review its clipboard handler and regenerate the unified
diff from pristine source (`diff -u` with `a/core/rfb.js` and `b/core/rfb.js`
labels). Update the release pin in the Containerfile and viewer recipe, the two
file hashes in the wrapper, and this guide together. Run the real-browser
[outbound clipboard checks](../../tests/browser/outbound_clipboard.py) for small,
unavailable and recovered copies against the new viewer before accepting it.
