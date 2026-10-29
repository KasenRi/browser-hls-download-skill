#!/usr/bin/env python3
"""Resume finite, unencrypted MPEG-TS HLS with aria2; publish a verified MP4."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urljoin, urlsplit


def atomic_json(path, value):
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


def parse_playlist(text, base_url):
    lines = text.lstrip('\ufeff').splitlines()
    if not lines or lines[0].strip() != '#EXTM3U':
        raise ValueError('Not an HLS playlist (#EXTM3U missing).')
    forbidden = ('#EXT-X-STREAM-INF:', '#EXT-X-MEDIA:', '#EXT-X-MAP:',
                 '#EXT-X-BYTERANGE:', '#EXT-X-PART:', '#EXT-X-GAP',
                 '#EXT-X-I-FRAMES-ONLY', '#EXT-X-DEFINE:')
    if any(line.startswith(forbidden) for line in lines):
        raise ValueError('Choose a complete MPEG-TS media playlist; master/fMP4/range/gap playlists are unsupported.')
    if '#EXT-X-ENDLIST' not in [line.strip() for line in lines]:
        raise ValueError('Playlist is still live or incomplete (#EXT-X-ENDLIST missing).')
    entries, duration = [], None
    for i, raw in enumerate(lines):
        line = raw.strip()
        if line.startswith('#EXT-X-KEY:') and line != '#EXT-X-KEY:METHOD=NONE':
            raise ValueError('Encrypted HLS is unsupported by this helper.')
        if line.startswith('#EXTINF:'):
            if duration is not None:
                raise ValueError('Missing segment URI after EXTINF.')
            duration = float(line.split(':', 1)[1].split(',', 1)[0])
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError('Invalid segment duration.')
        elif line and not line.startswith('#'):
            if duration is None:
                raise ValueError('Segment URI without EXTINF.')
            url = urljoin(base_url, line)
            if urlsplit(url).scheme not in ('https', 'http') or any(c in url for c in '\r\n\t'):
                raise ValueError('Only HTTP(S) media URLs without control characters are supported.')
            entries.append({'line': i, 'url': url, 'duration': duration})
            duration = None
    if duration is not None or not entries:
        raise ValueError('Empty or truncated media playlist.')
    return lines, entries


@contextmanager
def task_lock(work):
    with (work / 'run.lock').open('a+b') as lock:
        lock.seek(0, 2)
        if lock.tell() == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError('Another process owns this work directory.') from error
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == 'nt':
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


def executable(value, name):
    candidate = shutil.which(value or name)
    if candidate is None:
        option = 'aria2' if name == 'aria2c' else name
        raise ValueError(f'{name} not found; install it or pass --{option}.')
    return candidate


def managed_process(command, log, poll):
    flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    with log.open('ab') as stream:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                   stdout=stream, stderr=stream, **flags)
        try:
            while process.poll() is None:
                poll()
                time.sleep(2)
            poll()
            if process.returncode:
                raise RuntimeError(f'{Path(command[0]).name} exited with {process.returncode}; see {log.name}.')
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def probe(ffprobe, source):
    result = subprocess.run([ffprobe, '-v', 'error', '-protocol_whitelist', 'file,crypto,data',
        '-allowed_extensions', 'ALL', '-i', str(source), '-show_entries',
        'format=duration,size:stream=codec_name,codec_type,width,height', '-of', 'json'],
        capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def verify(ffmpeg, ffprobe, source, expected, tolerance):
    report = probe(ffprobe, source)
    duration = float(report['format']['duration'])
    if abs(duration - expected) > tolerance:
        raise RuntimeError(f'Duration mismatch: playlist={expected:.3f}s, output={duration:.3f}s.')
    if not any(s['codec_type'] == 'video' for s in report['streams']):
        raise RuntimeError('Output has no video stream.')
    checks = []
    for at in sorted({0.0, max(0.0, duration / 2 - 1), max(0.0, duration - 2)}):
        result = subprocess.run([ffmpeg, '-v', 'error', '-nostdin', '-xerror', '-ss', str(at),
            '-i', str(source), '-t', '2', '-map', '0:v:0', '-map', '0:a:0?',
            '-progress', 'pipe:1', '-f', 'null', '-'], capture_output=True, text=True)
        frames = re.findall(r'^frame=(\d+)$', result.stdout, re.MULTILINE)
        if result.returncode or not frames or int(frames[-1]) == 0:
            raise RuntimeError(f'Decode check failed at {at:.3f}s: {result.stderr[-1200:]}')
        checks.append({'at_seconds': at, 'frames': int(frames[-1])})
    report['decode_checks'] = checks
    report['expected_duration'] = expected
    return report


def run(args):
    ffmpeg, ffprobe, aria2 = (executable(args.ffmpeg, 'ffmpeg'),
                            executable(args.ffprobe, 'ffprobe'), executable(args.aria2, 'aria2c'))
    output = Path(args.output).expanduser().resolve()
    work = Path(args.work_dir).expanduser().resolve() if args.work_dir else output.with_name(output.stem + '.hls-work')
    work.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    with task_lock(work):
        if output.exists():
            raise ValueError('Output already exists; choose another --output path. No file was overwritten.')
        source_file, metadata_file = work / 'source.m3u8', work / 'task.json'
        existing = json.loads(metadata_file.read_text(encoding='utf-8')) if metadata_file.exists() else None
        temporary = output.with_name(output.stem + '.partial.mp4')
        if temporary.exists() and (existing or {}).get('partial_output') != str(temporary):
            raise ValueError('Temporary output already exists and is not owned by this task; choose another --output.')
        referer = args.referer or (existing or {}).get('referer')
        if referer and any(c in referer for c in '\r\n'):
            raise ValueError('Referer contains control characters.')
        base = args.url or args.base_url or (existing or {}).get('base_url')
        if not base:
            raise ValueError('Pass --url, or --playlist with --base-url when initializing a task.')
        if any(c in base for c in '\r\n\t'):
            raise ValueError('URL contains control characters.')
        context = ssl.create_default_context(cafile=args.ca_certificate)
        if args.playlist:
            text = Path(args.playlist).read_text(encoding='utf-8-sig')
        elif existing and source_file.exists():
            text = source_file.read_text(encoding='utf-8-sig')
        else:
            request = urllib.request.Request(base, headers={'User-Agent': 'Mozilla/5.0',
                **({'Referer': referer} if referer else {})})
            with urllib.request.urlopen(request, context=context, timeout=30) as response:
                text = response.read().decode('utf-8-sig')
                base = response.url
        lines, entries = parse_playlist(text, base)
        identity = [{'url': e['url'], 'duration': e['duration']} for e in entries]
        if existing and existing['segments'] != identity:
            raise ValueError('Playlist identity differs from saved task; use a new work directory.')
        metadata = {'base_url': base, 'segments': identity, 'referer': referer,
                    'partial_output': (existing or {}).get('partial_output')}
        atomic_json(metadata_file, metadata)
        source_file.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        parts = work / 'segments'
        parts.mkdir(exist_ok=True)
        started, expected = time.monotonic(), sum(e['duration'] for e in entries)
        state = {'pid': os.getpid(), 'total_segments': len(entries), 'expected_seconds': expected,
                 'output': str(output), 'work_dir': str(work)}

        def part(i):
            return parts / f'{i:06d}.ts'

        def inventory():
            files = [part(i) for i in range(len(entries))]
            return [p for p in files if p.exists() and p.stat().st_size > 0
                    and not Path(str(p) + '.aria2').exists()]

        def status(stage, **extra):
            complete = inventory()
            state.update(stage=stage, completed_segments=len(complete),
                         downloaded_bytes=sum(p.stat().st_size for p in complete),
                         updated_at=time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                         elapsed_seconds=round(time.monotonic() - started, 1), **extra)
            atomic_json(work / 'status.json', state)

        try:
            pending = []
            for i, entry in enumerate(entries):
                target = part(i)
                if not target.exists() or target.stat().st_size == 0 or Path(str(target) + '.aria2').exists():
                    pending.extend([entry['url'], f'  out={target.name}'])
            if pending:
                input_file = work / 'aria2-input.txt'
                input_file.write_text('\n'.join(pending) + '\n', encoding='utf-8')
                ca = Path(args.ca_certificate).resolve() if args.ca_certificate else work / 'trusted-roots.pem'
                if not args.ca_certificate:
                    roots = context.get_ca_certs(binary_form=True)
                    if roots:
                        ca.write_text(''.join(ssl.DER_cert_to_PEM_cert(c) for c in roots), encoding='ascii')
                command = [aria2, '--no-conf=true', '--enable-rpc=false', '--check-certificate=true',
                    '--input-file=' + str(input_file), '--dir=' + str(parts),
                    '--max-concurrent-downloads=' + str(args.jobs), '--max-connection-per-server=1',
                    '--split=1', '--file-allocation=none', '--auto-save-interval=5',
                    '--auto-file-renaming=false', '--allow-overwrite=false', '--continue=true',
                    '--timeout=30', '--connect-timeout=10', '--max-tries=' + str(args.retries),
                    '--retry-wait=2', '--summary-interval=10', '--enable-color=false', '--download-result=hide']
                if ca.exists():
                    command.append('--ca-certificate=' + str(ca))
                if referer:
                    command.append('--referer=' + referer)
                if args.min_speed:
                    command.append('--lowest-speed-limit=' + args.min_speed)
                status('downloading')
                print(f'Downloading/resuming {len(pending)//2} of {len(entries)} segments.', flush=True)
                managed_process(command, work / 'download.log', lambda: status('downloading'))
            if len(inventory()) != len(entries):
                raise RuntimeError('Segment inventory incomplete; keep the work directory and rerun.')
            for i in range(len(entries)):
                with part(i).open('rb') as stream:
                    header = stream.read(377)
                if len(header) < 377 or any(header[n] != 0x47 for n in (0, 188, 376)):
                    raise RuntimeError(f'Segment {i} is not supported MPEG-TS; keep it for inspection.')
            local_lines = list(lines)
            for i, entry in enumerate(entries):
                local_lines[entry['line']] = 'segments/' + part(i).name
            local_playlist = work / 'local.m3u8'
            local_playlist.write_text('\n'.join(local_lines) + '\n', encoding='utf-8')
            input_info = probe(ffprobe, local_playlist)
            if not any(s['codec_type'] == 'video' for s in input_info['streams']):
                raise RuntimeError('Selected playlist has no video; select a video media playlist.')
            if temporary.exists() and metadata.get('partial_output') != str(temporary):
                raise RuntimeError('Temporary output appeared while running; refusing to replace it.')
            metadata['partial_output'] = str(temporary)
            atomic_json(metadata_file, metadata)
            progress = work / 'merge-progress.txt'
            progress.write_text('', encoding='utf-8')
            command = [ffmpeg, '-hide_banner', '-loglevel', 'warning', '-nostdin', '-y',
                '-protocol_whitelist', 'file,crypto,data', '-allowed_extensions', 'ALL',
                '-i', str(local_playlist), '-map', '0:v:0', '-map', '0:a:0?', '-c', 'copy']
            if any(s['codec_name'] == 'hevc' for s in input_info['streams']):
                command.extend(['-tag:v', 'hvc1'])
            if any(s['codec_name'] == 'aac' for s in input_info['streams']):
                command.extend(['-bsf:a', 'aac_adtstoasc'])
            if args.faststart:
                command.extend(['-movflags', '+faststart'])
            command.extend(['-progress', str(progress), str(temporary)])

            def merge_status():
                fields = {}
                for line in progress.read_text(encoding='utf-8', errors='replace').splitlines():
                    key, sep, value = line.partition('=')
                    if sep:
                        fields[key] = value
                micros = fields.get('out_time_us', '0')
                status('merging', merged_seconds=int(micros) / 1_000_000 if micros.lstrip('-').isdigit() else 0,
                       merge_speed=fields.get('speed', 'N/A').strip())

            print('All segments downloaded; merging locally without re-encoding.', flush=True)
            managed_process(command, work / 'merge.log', merge_status)
            status('verifying')
            report = verify(ffmpeg, ffprobe, temporary, expected, args.duration_tolerance)
            atomic_json(work / 'validation.json', report)
            if output.exists():
                raise RuntimeError('Output appeared while running; refusing to replace it.')
            os.replace(temporary, output)
            status('complete', actual_seconds=float(report['format']['duration']), output_bytes=output.stat().st_size)
            print('Complete: ' + str(output), flush=True)
        except KeyboardInterrupt:
            status('paused')
            raise
        except Exception as error:
            status('error', error=str(error))
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--url', help='Finite MPEG-TS media playlist URL; saved locally for resume.')
    source.add_argument('--playlist', help='Saved media playlist; requires --base-url on first run.')
    parser.add_argument('--base-url', help='Original media playlist URL for relative segment resolution.')
    parser.add_argument('--output', required=True, help='Final MP4 path; published only after verification.')
    parser.add_argument('--work-dir', help='Resume/migration directory; defaults to <output-stem>.hls-work.')
    parser.add_argument('--aria2', help='aria2c executable, including a discovered Motrix engine path.')
    parser.add_argument('--ffmpeg', help='FFmpeg executable if not in PATH.')
    parser.add_argument('--ffprobe', help='FFprobe executable if not in PATH.')
    parser.add_argument('--jobs', type=int, default=16)
    parser.add_argument('--retries', type=int, default=10)
    parser.add_argument('--referer', help='Observed media request Referer, only when required.')
    parser.add_argument('--ca-certificate', help='Trusted PEM bundle; TLS checks remain enabled.')
    parser.add_argument('--min-speed', help='Optional aria2 speed floor, e.g. 100K; unset on slow links.')
    parser.add_argument('--faststart', action='store_true', help='Move MP4 index to front; adds disk IO.')
    parser.add_argument('--duration-tolerance', type=float, default=2.0)
    args = parser.parse_args(argv)
    if args.jobs < 1 or args.retries < 1 or not math.isfinite(args.duration_tolerance) or args.duration_tolerance < 0:
        parser.error('jobs/retries must be positive and duration-tolerance finite and nonnegative.')
    try:
        run(args)
        return 0
    except KeyboardInterrupt:
        print('Paused; rerun with the same work directory to resume.', file=sys.stderr)
        return 130
    except Exception as error:
        print('Error: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
