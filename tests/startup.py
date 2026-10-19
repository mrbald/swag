#!/usr/bin/env python3.12
"""Fault injection for the stack driver's startup gate; recreates the demo."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", default="podman")
    args = parser.parse_args()
    output = ROOT / "artifacts/startup-check"
    output.mkdir(parents=True, exist_ok=True)
    results = []
    try:
        for name in ("swag-bank", "swag-host", "swag-oauth"):
            app.down(args.engine)
            with (output / f"{name}.log").open("w") as log:
                process = subprocess.Popen([sys.executable, "tools/app.py", "up", "--engine", args.engine],
                                           cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
                try:
                    deadline = time.monotonic() + 45
                    injected = False
                    while time.monotonic() < deadline and process.poll() is None:
                        state = app.run(args.engine, "inspect", name, "--format", "{{.State.Running}}",
                                        check=False, capture=True)
                        if state.returncode == 0 and state.stdout.strip() == "true":
                            app.run(args.engine, "kill", name, capture=True)
                            injected = True
                            break
                        time.sleep(.08)
                    assert injected, f"Failed to inject {name} startup failure"
                    code = process.wait(timeout=45)
                    text = (output / f"{name}.log").read_text()
                    assert code != 0 and name in text and "Workspace:" not in text, text
                    results.append({"component": name, "status": "pass", "startup_exit": code})
                    print(f"startup: {name} failure rejected: pass", flush=True)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=10)
        app.up(args.engine, 6081)
        results.append({"component": "healthy stack", "status": "pass"})
    finally:
        (output / "results.json").write_text(json.dumps({"checks": results}, indent=2) + "\n")


if __name__ == "__main__":
    main()
