#!/usr/bin/env python3
"""Build/run the private rendering probe. All owned containers use swag-* names."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "localhost/swag-probe:dev"


def command(engine: str, *args: str, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run([engine, *args], check=True, text=True,
                          capture_output=capture, cwd=ROOT)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["build", "run", "stop"])
    parser.add_argument("--engine", default=os.getenv("SWAG_CONTAINER_ENGINE", "podman"))
    parser.add_argument("--backend", choices=["qt", "tiger", "qt-ws", "qt-unix"], default="tiger")
    parser.add_argument("--transport", choices=["tcp", "unix", "websocket"])
    parser.add_argument("--name", default="swag-probe")
    parser.add_argument("--port", type=int, default=6080)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.name.startswith("swag-"):
        parser.error("container names must start with swag-")
    if not shutil.which(args.engine):
        parser.error(f"container engine not found: {args.engine}")
    image = "localhost/swag-probe-ws:dev" if args.backend in {"qt-ws", "qt-unix"} else IMAGE
    if args.action == "build":
        command(args.engine, "build", "--target", "probe-ws" if args.backend in {"qt-ws", "qt-unix"} else "probe", "-f", "containers/Containerfile", "-t", image, ".")
        return 0
    if args.action == "stop":
        command(args.engine, "stop", "--time", "5", args.name)
        command(args.engine, "rm", args.name)
        return 0
    if not 1 <= args.port <= (65534 if args.backend == "qt-ws" else 65535):
        parser.error("port must be valid and leave room for the qt-ws WebSocket port (+1)")
    transport = args.transport or ("websocket" if args.backend == "qt-ws" else
                                   "unix" if args.backend in {"tiger", "qt-unix"} else "tcp")
    if args.backend == "qt-ws" and transport != "websocket":
        parser.error("qt-ws supports WebSocket transport only")
    if args.backend != "qt-ws" and transport == "websocket":
        parser.error("WebSocket transport requires qt-ws")
    if args.backend == "qt-unix" and transport != "unix":
        parser.error("qt-unix requires Unix RFB transport")
    if args.backend == "qt" and transport == "unix":
        parser.error("the unpatched Qt VNC plugin supports TCP only")
    output = (args.output or ROOT / "artifacts" / f"{time.strftime('%Y%m%d-%H%M%S')}-{args.backend}").resolve()
    output.mkdir(parents=True, exist_ok=True)
    generation = uuid.uuid4().hex
    image_id = command(args.engine, "image", "inspect", image, "--format", "{{.Id}}", capture=True).stdout.strip()
    user_args = (["--userns", "keep-id:uid=1000,gid=1000"] if Path(args.engine).name == "podman"
                 else ["--user", f"{os.getuid()}:{os.getgid()}"])
    websocket_port = args.port + 1
    extra_ports = (["--publish", f"127.0.0.1:{websocket_port}:5900"]
                   if args.backend == "qt-ws" else [])
    result = command(args.engine, "run", "--detach", "--name", args.name, *user_args,
                     "--publish", f"127.0.0.1:{args.port}:6080", *extra_ports,
                     "--volume", f"{output}:/artifacts:Z",
                     "--env", f"SWAG_BACKEND={args.backend}",
                     "--env", f"SWAG_RFB_TRANSPORT={transport}",
                     "--env", "SWAG_USER=probe",
                     "--env", f"SWAG_GENERATION={generation}", image, capture=True)
    manifest = {"container": args.name, "container_id": result.stdout.strip(),
                "backend": args.backend, "transport": transport, "generation": generation, "image_id": image_id,
                "url": f"http://127.0.0.1:{args.port}/probe.html" + (f"?wsPort={websocket_port}" if args.backend == "qt-ws" else ""), "artifacts": str(output),
                "scope": "private rendering probe; no authentication or per-user ownership yet"}
    (output / "run.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
