"""Local synthetic media tests: no browser session, credentials or real replay."""
from collections import Counter
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'hls_download.py'
SPEC = importlib.util.spec_from_file_location('hls_download', SCRIPT)
HLS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HLS)


class PlaylistTests(unittest.TestCase):
    def test_relative_urls_and_discontinuity(self):
        text = '#EXTM3U\n#EXTINF:1.25,\n../a.ts?part=1\n#EXT-X-DISCONTINUITY\n#EXTINF:2,\n/b.ts\n#EXT-X-ENDLIST\n'
        lines, entries = HLS.parse_playlist(text, 'https://media.example/video/list.m3u8')
        self.assertEqual([e['url'] for e in entries], ['https://media.example/a.ts?part=1', 'https://media.example/b.ts'])
        self.assertAlmostEqual(sum(e['duration'] for e in entries), 3.25)
        self.assertIn('#EXT-X-DISCONTINUITY', lines)

    def test_refuse_unsupported_and_truncated_inputs(self):
        samples = [
            '#EXTM3U\n#EXTINF:1,\na.ts\n',
            '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100\nvideo.m3u8\n#EXT-X-ENDLIST',
            '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key"\n#EXTINF:1,\na.ts\n#EXT-X-ENDLIST',
            '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:1,\na.m4s\n#EXT-X-ENDLIST',
            '#EXTM3U\n#EXT-X-BYTERANGE:100@0\n#EXTINF:1,\na.ts\n#EXT-X-ENDLIST',
            '#EXTM3U\n#EXTINF:NaN,\na.ts\n#EXT-X-ENDLIST',
            '#EXTM3U\n#EXTINF:1,\n#EXT-X-ENDLIST',
        ]
        for text in samples:
            with self.subTest(text=text), self.assertRaises(ValueError):
                HLS.parse_playlist(text, 'https://media.example/list.m3u8')


class DownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.aria2 = os.environ.get('HLS_TEST_ARIA2') or shutil.which('aria2c')
        cls.ffmpeg = shutil.which('ffmpeg')
        cls.ffprobe = shutil.which('ffprobe')
        if not all((cls.aria2, cls.ffmpeg, cls.ffprobe)):
            raise unittest.SkipTest('Requires aria2c (or HLS_TEST_ARIA2), ffmpeg and ffprobe.')
        cls.temp = tempfile.TemporaryDirectory(prefix='hls-skill-test-')
        cls.root = Path(cls.temp.name)
        cls.media = cls.root / 'served media'
        cls.media.mkdir()
        subprocess.run([cls.ffmpeg, '-v', 'error', '-nostdin', '-y', '-f', 'lavfi',
            '-i', 'testsrc2=size=160x96:rate=10', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
            '-t', '4', '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
            '-g', '10', '-sc_threshold', '0', '-c:a', 'aac', '-f', 'hls', '-hls_time', '1',
            '-hls_list_size', '0', '-hls_segment_filename', str(cls.media / 'chunk%02d.ts'),
            str(cls.media / 'index.m3u8')], check=True, capture_output=True)
        cls.requests = Counter()
        cls.denied = set()
        cls.require_referer = None

        class Handler(SimpleHTTPRequestHandler):
            def do_GET(self):
                path = unquote(urlsplit(self.path).path)
                cls.requests[path] += 1
                if cls.require_referer and self.headers.get('Referer') != cls.require_referer:
                    self.send_error(403)
                elif path in cls.denied:
                    self.send_error(404)
                else:
                    super().do_GET()

            def log_message(self, *args):
                pass

        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(cls.media)))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}/index.m3u8'

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, 'server'):
            cls.server.shutdown()
            cls.server.server_close()
            cls.thread.join(timeout=5)
            cls.temp.cleanup()

    def invoke(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), '--aria2', self.aria2,
            '--jobs', '2', '--retries', '1', *map(str, args)], capture_output=True, text=True, timeout=60)

    def test_failed_download_then_migrate_and_resume(self):
        self.requests.clear()
        self.denied.add('/chunk02.ts')
        type(self).require_referer = 'https://player.example/replay'
        old_work = self.root / 'old machine' / 'video.hls-work'
        output = self.root / 'published video.mp4'
        try:
            first = self.invoke('--url', self.url, '--referer', self.require_referer,
                                '--work-dir', old_work, '--output', output)
            self.assertNotEqual(first.returncode, 0, first.stdout)
            self.assertFalse(output.exists())
            self.assertEqual(json.loads((old_work / 'status.json').read_text())['stage'], 'error')
            downloaded = [p for p in (old_work / 'segments').glob('*.ts')
                          if p.stat().st_size and not Path(str(p) + '.aria2').exists()]
            self.assertEqual(len(downloaded), 3)
            before = dict(self.requests)
        finally:
            self.denied.clear()
        moved_work = self.root / 'new machine' / 'video.hls-work'
        moved_work.parent.mkdir()
        shutil.move(str(old_work), str(moved_work))
        second = self.invoke('--work-dir', moved_work, '--output', output, '--faststart')
        type(self).require_referer = None
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertTrue(output.exists())
        self.assertFalse(output.with_name('published video.partial.mp4').exists())
        status = json.loads((moved_work / 'status.json').read_text())
        self.assertEqual(status['stage'], 'complete')
        self.assertEqual(status['completed_segments'], 4)
        self.assertAlmostEqual(status['actual_seconds'], 4, delta=0.15)
        report = json.loads((moved_work / 'validation.json').read_text())
        self.assertEqual({s['codec_type'] for s in report['streams']}, {'video', 'audio'})
        self.assertEqual(len(report['decode_checks']), 3)
        for path in ('/chunk00.ts', '/chunk01.ts', '/chunk03.ts', '/index.m3u8'):
            self.assertEqual(self.requests[path], before[path], path + ' was fetched again')
        self.assertEqual(self.requests['/chunk02.ts'], before['/chunk02.ts'] + 1)
        third = self.invoke('--work-dir', moved_work, '--output', output)
        self.assertNotEqual(third.returncode, 0)
        self.assertIn('already exists', third.stderr)

    def test_no_publish_when_duration_check_fails(self):
        self.requests.clear()
        playlist = self.root / 'wrong-duration.m3u8'
        text = (self.media / 'index.m3u8').read_text()
        text = text.replace('#EXTINF:1.000000,', '#EXTINF:3.000000,')
        playlist.write_text(text)
        work, output = self.root / 'wrong.hls-work', self.root / 'wrong.mp4'
        result = self.invoke('--playlist', playlist, '--base-url', self.url,
                             '--work-dir', work, '--output', output)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Duration mismatch', result.stderr)
        self.assertFalse(output.exists())
        self.assertTrue(output.with_name('wrong.partial.mp4').exists())
        self.assertEqual(json.loads((work / 'status.json').read_text())['stage'], 'error')

    def test_refuse_changed_identity_and_simultaneous_writer(self):
        work = self.root / 'identity.hls-work'
        work.mkdir()
        text = (self.media / 'index.m3u8').read_text()
        lines, entries = HLS.parse_playlist(text, self.url)
        (work / 'source.m3u8').write_text(text)
        (work / 'task.json').write_text(json.dumps({'base_url': self.url,
            'segments': [{'url': e['url'], 'duration': e['duration']} for e in entries]}))
        changed = self.root / 'changed.m3u8'
        changed.write_text(text.replace('chunk00.ts', 'different.ts'))
        result = self.invoke('--playlist', changed, '--base-url', self.url,
                             '--work-dir', work, '--output', self.root / 'identity.mp4')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('identity differs', result.stderr)
        with HLS.task_lock(work):
            result = self.invoke('--work-dir', work, '--output', self.root / 'identity.mp4')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Another process', result.stderr)
        unrelated = self.root / 'identity.partial.mp4'
        unrelated.write_bytes(b'unrelated existing file')
        result = self.invoke('--work-dir', work, '--output', self.root / 'identity.mp4')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not owned', result.stderr)
        self.assertEqual(unrelated.read_bytes(), b'unrelated existing file')


if __name__ == '__main__':
    unittest.main()
