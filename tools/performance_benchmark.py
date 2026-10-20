#!/usr/bin/env python3
"""Capture one process's Metal HUD, preserve raw logs, export auditable frame metrics."""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timezone


def percentile(values, p):
    a = sorted(values)
    at = (len(a) - 1) * p
    lo, hi = math.floor(at), math.ceil(at)
    return a[lo] + (a[hi] - a[lo]) * (at - lo)


def parse_hud(lines):
    frames, issues = [], []
    previous = None
    packets = 0
    for line_number, raw in enumerate(lines, 1):
        if raw.startswith('Filtering the log data'):
            continue
        stamp = None
        try:
            event = json.loads(raw)
            message = event.get('eventMessage', event.get('composedMessage', ''))
            stamp = event.get('timestamp', event.get('date'))
        except (ValueError, AttributeError):
            message = raw
        if not isinstance(message, str) or 'metal-HUD:' not in message:
            continue
        payload = message.split('metal-HUD:', 1)[1].strip()
        if not re.match(r'^\d+\s*,', payload):
            issues.append(f'line {line_number}: unknown HUD packet')
            continue
        try:
            tokens = payload.split(',')
            first = int(tokens[0])
            # Apple documents three header values, followed by interval/GPU pairs.
            values = [float(x.strip()) for x in tokens[1:]]
            if len(values) < 4 or (len(values) - 2) % 2 or not all(math.isfinite(v) for v in values):
                raise ValueError('invalid size or number')
            pairs = list(zip(values[2::2], values[3::2]))
            if any(interval < 0 or gpu < 0 for interval, gpu in pairs):
                raise ValueError('negative timing')
        except (ValueError, OverflowError):
            issues.append(f'line {line_number}: malformed HUD packet')
            continue
        packets += 1
        for offset, (interval, gpu) in enumerate(pairs):
            number = first + offset
            if previous is not None and number != previous + 1:
                issues.append(f'line {line_number}: discontinuous frame IDs {previous} -> {number}')
            previous = number
            if interval == 0:
                issues.append(f'frame {number}: zero interval; not included in timing metrics')
                continue
            frames.append(dict(frame=number, interval_ms=interval, gpu_ms=gpu, packet_timestamp=stamp))
    return frames, issues, packets


def summarize(frames):
    if not frames:
        return dict(frame_intervals=0)
    times = [r['interval_ms'] for r in frames]
    duration = sum(times) / 1000
    def low(fraction):
        slowest = sorted(times, reverse=True)[:max(1, math.ceil(len(times) * fraction))]
        return 1000 / (sum(slowest) / len(slowest))
    return dict(frame_intervals=len(times), duration_from_intervals_s=duration,
                average_fps=len(times) / duration, low_1_percent_fps=low(.01),
                low_0_1_percent_fps=low(.001),
                frame_time_ms={name:percentile(times, p) for name,p in [('p50',.5),('p95',.95),('p99',.99),('p99_9',.999)]},
                max_frame_time_ms=max(times), gpu_time_p95_ms=percentile([r['gpu_ms'] for r in frames],.95),
                stutters={str(limit):dict(count=sum(t > limit for t in times),
                    per_minute=sum(t > limit for t in times) * 60 / duration)
                    for limit in [33.3333333333,50,100]})


def analyze(raw_path, output, windows_path=None, manifest_path=None):
    data = raw_path.read_bytes()
    frames, issues, packets = parse_hud(data.decode('utf-8', errors='replace').splitlines())
    windows = json.loads(windows_path.read_text()) if windows_path else []
    for i, window in enumerate(windows):
        if not window.get('label') or window['first_frame'] > window['last_frame']:
            raise ValueError('Every window needs a label and an ordered frame range')
        if i and window['first_frame'] <= windows[i-1]['last_frame']:
            raise ValueError('Windows must be sorted and non-overlapping')
        if not frames or window['first_frame'] < frames[0]['frame'] or window['last_frame'] > frames[-1]['frame']:
            raise ValueError('Window outside captured frame range')
    selected = [r for r in frames if not windows or any(w['first_frame'] <= r['frame'] <= w['last_frame'] for w in windows)]
    manifest = json.loads(manifest_path.read_text()) if manifest_path else {}
    required = ['variant','binary_sha256','game_build','renderer','scenario','settings','cache_state','cache_preparation','power_display','visual_check','hud_settings','repeat_number','variant_order','scene_bounds_and_exclusions']
    missing = [key for key in required if not manifest.get(key)]
    if manifest and (not isinstance(manifest.get('visual_check'), dict) or manifest['visual_check'].get('passed') is not True):
        issues.append('Visual correctness has not passed a documented check')
    output.mkdir(parents=True, exist_ok=True)
    with (output/'frames.csv').open('w', newline='') as out:
        writer=csv.DictWriter(out,fieldnames=['frame','interval_ms','gpu_ms','packet_timestamp']);writer.writeheader();writer.writerows(frames)
    report=dict(source='Apple Metal Performance HUD (documented three-header CSV packet)',
                raw_sha256=hashlib.sha256(data).hexdigest(),packets=packets,issues=issues,
                selected_windows=windows,manifest=manifest,missing_manifest_fields=missing,
                eligible_for_paired_review=bool(selected and windows and not issues and not missing),
                metrics=summarize(selected),
                formulas=dict(average_fps='interval count / sum(interval seconds)',
                              low_fps='1000 / arithmetic mean of slowest ceil(N * fraction) frame intervals in ms',
                              percentiles='linear interpolation at (N-1)*p',
                              stutter='one frame interval strictly above threshold; not necessarily shader compilation'),
                caveat='A complete capture is one run, not proof of improvement. Compare matched repetitions; validate visual output, scene, caches, focus and measurement overhead.')
    (output/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    return report


def capture(pid, seconds, output):
    output.mkdir(parents=True, exist_ok=False)
    predicate=f'processIdentifier == {pid} AND (composedMessage CONTAINS "metal-HUD:" OR subsystem == "com.apple.metal.hud")'
    command=['/usr/bin/log','stream','--style','ndjson','--level','debug','--predicate',predicate]
    start=datetime.now(timezone.utc).isoformat()
    with (output/'raw.ndjson').open('wb') as out, (output/'capture.stderr').open('wb') as err:
        process=subprocess.Popen(command,stdout=out,stderr=err)
        try:
            code=process.wait(timeout=seconds)
            complete=False
        except subprocess.TimeoutExpired:
            complete=True
            process.terminate()
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.wait()
    (output/'capture.json').write_text(json.dumps(dict(pid=pid,requested_duration_s=seconds,start_utc=start,
        end_utc=datetime.now(timezone.utc).isoformat(),completed_requested_window=complete,command=command),indent=2)+'\n')
    report=analyze(output/'raw.ndjson',output)
    if not complete:
        raise RuntimeError('Logger ended before the requested window; see capture.stderr. This is not a complete benchmark capture.')
    if not report['packets']:
        raise RuntimeError('No valid Metal HUD frames captured. Confirm HUD/log environment at process startup; this is not a benchmark result.')
    print(json.dumps(report['metrics'],indent=2))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    a=commands.add_parser('analyze');a.add_argument('raw',type=Path);a.add_argument('--output',type=Path,required=True);a.add_argument('--windows',type=Path);a.add_argument('--manifest',type=Path)
    c=commands.add_parser('capture');c.add_argument('--pid',type=int,required=True);c.add_argument('--seconds',type=float,default=120);c.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.command=='capture':
        if args.pid<=0 or not 1<=args.seconds<=1800:parser.error('Use a positive process PID and 1..1800 seconds')
        capture(args.pid,args.seconds,args.output)
    else:print(json.dumps(analyze(args.raw,args.output,args.windows,args.manifest)['metrics'],indent=2))

if __name__=='__main__':
    main()
