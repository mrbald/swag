#!/usr/bin/env python3
"""Compare two plugin images using one EL8-built paint/composite benchmark binary.

Set CONTAINER_CONNECTION when selecting a nondefault Podman connection. This
synthetic check has no VNC client and measures neither encoding nor network cost.
"""
from __future__ import annotations
import argparse
import json
import math
from pathlib import Path
import statistics
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', default='podman')
    parser.add_argument('--baseline-image', required=True)
    parser.add_argument('--candidate-image', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--iterations', type=int, default=2000)
    parser.add_argument('--samples', type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.iterations <= 1000000 or not 1 <= args.samples <= 100:
        parser.error('iterations must be 1..1000000 and samples 1..100')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    name = 'swag-paint-bench-' + uuid.uuid4().hex[:8]

    def command(label, *arguments, timeout=60):
        result = subprocess.run([args.engine, *arguments], cwd=ROOT,
                                capture_output=True, text=True, timeout=timeout)
        (output/(label+'.log')).write_text(result.stdout + result.stderr)
        result.check_returncode()
        return result.stdout

    command('build', 'run', '--rm', '--name', name, '--user', '0',
            '--entrypoint', '/bin/sh', '--volume', f'{ROOT}:/src:ro',
            '--volume', f'{output}:/out:rw', args.baseline_image, '-c',
            'c++ -std=c++23 -O2 -fPIC /src/tests/fixtures/vncws_paint_benchmark.cpp '
            '-o /out/vncws-paint-benchmark $(pkg-config --cflags --libs Qt5Widgets)')
    images = {'baseline': args.baseline_image, 'candidate': args.candidate_image}
    identity = {label: command(label+'-image', 'image', 'inspect', image,
                              '--format', '{{.Id}}').strip()
                for label, image in images.items()}
    samples = []
    report = {'images': identity, 'samples': samples,
              'scope': 'same binary, synthetic GUI-thread paint+composite, no VNC client'}
    for workload in ('small', 'full'):
        for sample in range(args.samples):
            # Alternate order to reduce bias from startup/thermal drift.
            order = ('baseline', 'candidate') if sample % 2 == 0 else ('candidate', 'baseline')
            for label in order:
                text = command(f'{workload}-{sample}-{label}', 'run', '--rm',
                               '--name', name, '--network', 'none',
                               '--entrypoint', '/out/vncws-paint-benchmark',
                               '--volume', f'{output}:/out:ro',
                               '--env', 'QT_PLUGIN_PATH=/opt/swag/plugins',
                               '--env', 'QT_QPA_PLATFORM=vncws:size=1280x800:port=5900',
                               '--env', 'QT_LOGGING_RULES=qt.qpa.*=false',
                               images[label], workload, str(args.iterations))
                result = json.loads(text.strip().splitlines()[-1])
                result.update(variant=label, sample=sample)
                samples.append(result)
                print(f"{workload} {sample} {label}: "
                      f"{result['wall_ns']/result['iterations']/1000:.3f} us/update", flush=True)
                (output/'results.json').write_text(json.dumps(report, indent=2)+'\n')
    summary = {}
    for workload in ('small', 'full'):
        medians = {}
        for label in images:
            rows = [r for r in samples if r['workload'] == workload and r['variant'] == label]
            wall = sorted(r['wall_ns']/r['iterations']/1000 for r in rows)
            medians[label] = {
                'wall_run_mean_p95_us': wall[math.ceil(.95 * len(wall)) - 1],
                'wall_min_us': wall[0], 'wall_max_us': wall[-1],
                'wall_us': statistics.median(r['wall_ns']/r['iterations']/1000 for r in rows),
                'cpu_us': statistics.median(r['cpu_seconds']/r['iterations']*1e6 for r in rows)}
        medians['wall_change_percent'] = 100 * (medians['candidate']['wall_us']/medians['baseline']['wall_us']-1)
        summary[workload] = medians
    report['summary'] = summary
    (output/'results.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
