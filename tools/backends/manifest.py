#!/usr/bin/env python3.12
"""Print the actual image/runtime versions; optionally retain full Xvnc help."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess


def run(*command: str) -> str:
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            check=False, text=True)
    return result.stdout.strip()


def digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    xvnc_help = run("Xvnc", "-help")
    plugins = Path(run("qmake-qt5", "-query", "QT_INSTALL_PLUGINS"))
    manifest = {
        "baseImage": os.environ.get("SWAG_BASE_IMAGE"),
        "architecture": platform.machine(),
        "osRelease": Path("/etc/os-release").read_text(),
        "compiler": {"path": shutil.which("g++"), "version": run("g++", "--version")},
        "cmake": run("cmake", "--version").splitlines()[0],
        "ninja": run("ninja-build", "--version"),
        "qt": {
            "version": run("qmake-qt5", "-query", "QT_VERSION"),
            "plugins": str(plugins),
            "vncPluginSha256": digest(plugins / "platforms/libqvnc.so"),
            "xcbPluginSha256": digest(plugins / "platforms/libqxcb.so"),
        },
        "python": platform.python_version(),
        "tigerVnc": run("rpm", "-q", "tigervnc-server-minimal"),
        "tigerHelp": {
            "rfbunixpath": "rfbunixpath" in xvnc_help.lower(),
            "rfbport": "rfbport" in xvnc_help.lower(),
            "sha256": hashlib.sha256(xvnc_help.encode()).hexdigest(),
        },
        "openbox": run("rpm", "-q", "openbox"),
        "noVnc": {
            "version": Path("/opt/novnc/SWAG_VERSION").read_text().strip(),
            "sourceSha256": Path("/opt/novnc/SWAG_SOURCE_SHA256").read_text().strip(),
        },
        "websockify": importlib.metadata.version("websockify"),
        "probeSha256": digest(Path("/opt/swag/bin/swag-probe")),
        "packages": run("rpm", "-qa", "--qf", "%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}\n").splitlines(),
    }
    manifest["packages"].sort()
    encoded = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "manifest.json").write_text(encoded)
        (args.output_dir / "xvnc-help.txt").write_text(xvnc_help + "\n")
    print(encoded, end="")


if __name__ == "__main__":
    main()
