import hashlib
import http.server
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.download_manager import download_verified


class DownloadTests(unittest.TestCase):
    def test_interrupted_resume_mirror_cancel_and_size_bound(self):
        payload = b'bounded-real-http-' * 20000
        offsets = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                if self.path == '/missing':
                    self.send_error(404)
                    return
                offset = int(self.headers.get('Range', 'bytes=0-')[6:-1])
                offsets.append(offset)
                body = payload[offset:]
                self.send_response(206 if offset else 200)
                if offset:
                    self.send_header('Content-Range', 'bytes %d-%d/%d' % (offset, len(payload)-1, len(payload)))
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                if self.path == '/interrupted' and offset == 0:
                    self.wfile.write(body[:65536])
                    self.wfile.flush()
                    self.close_connection = True
                    return
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / 'asset.bin'
                url = 'http://127.0.0.1:%d' % server.server_port
                sha = hashlib.sha256(payload).hexdigest()
                options = {'allowed_hosts': ['127.0.0.1'], 'allow_http_loopback': True,
                           'retries': 1, 'timeout': 5}
                result = download_verified(url+'/interrupted', target, sha, **options)
                self.assertEqual(result['attempts'], 2)
                self.assertIn(65536, offsets)
                self.assertEqual(target.read_bytes(), payload)
                target.unlink()
                result = download_verified([url+'/missing', url+'/ok'], target, sha, **options)
                self.assertEqual(result['attempts'], 3)
                target.unlink()
                progress = []
                class Cancelled(RuntimeError):
                    pass
                def cancel():
                    if progress:
                        raise Cancelled('user stopped download')
                with self.assertRaises(Cancelled):
                    download_verified(url+'/ok', target, sha, progress=progress.append,
                                      check_cancel=cancel, **options)
                self.assertFalse(target.exists())
                self.assertEqual(Path(str(target)+'.download.part').stat().st_size, 65536)
                self.assertTrue(download_verified(url+'/ok', target, sha, **options)['complete'])
                with self.assertRaises(ValueError):
                    download_verified(url+'/ok', target, sha, max_bytes=100, **options)
                target.unlink()
                with self.assertRaises(RuntimeError):
                    download_verified(url+'/ok', target, sha, max_bytes=100, **options)
                self.assertFalse(target.exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_range_resume_ignored_range_bad_hash_and_host_policy(self):
        payload = b'real-network-fixture-' * 8000
        ranges = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                if self.path == '/redirect':
                    self.send_response(302)
                    self.send_header('Location', 'http://unapproved.invalid/file')
                    self.end_headers()
                    return
                offset = int(self.headers.get('Range', 'bytes=0-')[6:-1])
                ranges.append(offset)
                partial = offset > 0 and self.path != '/ignore-range'
                body = payload[offset:] if partial else payload
                self.send_response(206 if partial else 200)
                if partial:
                    self.send_header('Content-Range', 'bytes %d-%d/%d' % (offset, len(payload)-1, len(payload)))
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / 'asset.bin'
                sha = hashlib.sha256(payload).hexdigest()
                url = 'http://127.0.0.1:%d' % server.server_port
                options = {'allowed_hosts': ['127.0.0.1'], 'allow_http_loopback': True,
                           'retries': 0, 'timeout': 5}
                for endpoint in ('/range', '/ignore-range'):
                    if target.exists():
                        target.unlink()
                    Path(str(target)+'.download.part').write_bytes(payload[:500])
                    Path(str(target)+'.download.json').write_text(json.dumps({'sha256': sha}), encoding='utf-8')
                    result = download_verified(url+endpoint, target, sha, **options)
                    self.assertTrue(result['network'])
                    self.assertEqual(target.read_bytes(), payload)
                self.assertIn(500, ranges)
                self.assertTrue(download_verified(url+'/range', target, sha, **options)['cache_hit'])
                target.write_bytes(b'keep old asset')
                with self.assertRaises(RuntimeError):
                    download_verified(url+'/range', target, '0'*64, **options)
                self.assertEqual(target.read_bytes(), b'keep old asset')
                with self.assertRaises(RuntimeError):
                    download_verified(url+'/redirect', target, sha, **options)
                with self.assertRaises(ValueError):
                    download_verified(url+'/range', target, sha, allowed_hosts=['127.0.0.1'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
