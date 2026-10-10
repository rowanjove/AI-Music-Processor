"""Bounded, hash-verified network downloads into a caller-selected cache.

Sources and redirect hosts must be explicitly allowed. No source discovery,
package installation, or credentials logging occurs here.
"""
import hashlib
import http.client
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def download_verified(urls, destination, expected_sha256, allowed_hosts, *,
                      timeout=30, retries=2, max_bytes=2 * 1024 ** 3, proxy=None,
                      progress=None, check_cancel=None, allow_http_loopback=False):
    from core.install_platform import _plain_path
    if isinstance(urls, str):
        urls = [urls]
    if not urls or not isinstance(expected_sha256, str) or not re.fullmatch('[0-9a-f]{64}', expected_sha256):
        raise ValueError('A source and complete expected SHA-256 are required')
    if type(retries) is not int or not 0 <= retries <= 2 or not 0 < timeout <= 300:
        raise ValueError('Invalid bounded retry/timeout policy')
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError('max_bytes must be a positive integer')
    hosts = {host.lower() for host in allowed_hosts}

    def validate(url):
        parsed = urllib.parse.urlsplit(url)
        local = parsed.hostname in ('127.0.0.1', 'localhost', '::1')
        if (parsed.hostname not in hosts or parsed.username is not None or parsed.password is not None
                or parsed.fragment or (parsed.scheme != 'https' and not
                (allow_http_loopback and local and parsed.scheme == 'http'))):
            raise ValueError('Download source/redirect is outside the explicit HTTPS host policy')
        return url

    for url in urls:
        validate(url)
    class SafeRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, response, code, message, headers, new_url):
            try:
                validate(new_url)
            except ValueError:
                response.close()
                raise
            return super().redirect_request(request, response, code, message, headers, new_url)
    if proxy is not None:
        parsed_proxy = urllib.parse.urlsplit(proxy)
        if parsed_proxy.scheme not in ('http', 'https') or not parsed_proxy.hostname:
            raise ValueError('Invalid explicit HTTP/HTTPS proxy')
    opener = urllib.request.build_opener(SafeRedirect(), urllib.request.ProxyHandler(
        {'https': proxy, 'http': proxy} if proxy else {}))
    destination = str(_plain_path(destination))
    part, state = destination + '.download.part', destination + '.download.json'
    _plain_path(part)
    _plain_path(state)
    Path(destination).parent.mkdir(parents=True, exist_ok=True)

    def digest(path):
        result = hashlib.sha256()
        with open(path, 'rb') as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b''):
                result.update(block)
        return result.hexdigest()

    if os.path.isfile(destination) and digest(destination) == expected_sha256:
        if os.path.getsize(destination) > max_bytes:
            raise ValueError('Cached download exceeds byte budget')
        return {'complete': True, 'network': False, 'cache_hit': True,
                'destination': destination, 'sha256': expected_sha256, 'bytes': os.path.getsize(destination)}
    previous = None
    if os.path.isfile(state):
        try:
            with open(state, encoding='utf-8') as handle:
                previous = json.load(handle)
        except (OSError, ValueError):
            previous = None
    if previous != {'sha256': expected_sha256} and os.path.isfile(part):
        # Keep an unrecognized partial rather than mixing it into another asset.
        import uuid
        os.replace(part, part + '.previous.' + uuid.uuid4().hex)
    with open(state, 'w', encoding='utf-8') as handle:
        json.dump({'sha256': expected_sha256}, handle)
    attempts, last_error = 0, None
    for url in urls:
        for attempt in range(retries + 1):
            attempts += 1
            if check_cancel:
                check_cancel()
            _plain_path(part)
            offset = os.path.getsize(part) if os.path.isfile(part) else 0
            if offset > max_bytes:
                raise ValueError('Partial download exceeds byte budget')
            request = urllib.request.Request(url, headers={'Accept-Encoding': 'identity'})
            if offset:
                request.add_header('Range', 'bytes=%d-' % offset)
            try:
                response = opener.open(request, timeout=timeout)
                with response:
                    validate(response.geturl())
                    code = response.getcode()
                    if response.headers.get('Content-Encoding', 'identity') != 'identity':
                        raise ValueError('Unexpected compressed transfer encoding')
                    total = None
                    if code == 206:
                        match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
                        if not match or int(match[1]) != offset or int(match[2]) < offset or int(match[3]) <= int(match[2]):
                            raise ValueError('Invalid resume Content-Range')
                        total = int(match[3])
                        mode = 'ab'
                    elif code == 200:
                        # Server ignored Range: restart, never append a full body.
                        offset, mode = 0, 'wb'
                        length = response.headers.get('Content-Length')
                        if length is not None:
                            total = int(length)
                    else:
                        raise ValueError('Unexpected HTTP status')
                    if total is not None and (total < 0 or total > max_bytes):
                        raise ValueError('Download exceeds declared byte budget')
                    with open(part, mode) as handle:
                        received = offset
                        while True:
                            if check_cancel:
                                check_cancel()
                            block = response.read(64 * 1024)
                            if not block:
                                break
                            received += len(block)
                            if received > max_bytes or (total is not None and received > total):
                                raise ValueError('Download exceeds byte budget')
                            handle.write(block)
                            if progress:
                                progress({'received_bytes': received, 'total_bytes': total,
                                          'fraction': received / total if total else None})
                        handle.flush()
                        os.fsync(handle.fileno())
                    if total is not None and received != total:
                        raise OSError('Incomplete download body')
                if digest(part) != expected_sha256:
                    os.remove(part)
                    raise ValueError('Downloaded SHA-256 mismatch')
                _plain_path(destination)
                os.replace(part, destination)
                os.remove(state)
                return {'complete': True, 'network': True, 'cache_hit': False, 'attempts': attempts,
                        'destination': destination, 'sha256': expected_sha256, 'bytes': received}
            except urllib.error.HTTPError as error:
                error.close()
                if error.code == 416 and os.path.isfile(part):
                    if digest(part) == expected_sha256:
                        _plain_path(destination)
                        os.replace(part, destination)
                        os.remove(state)
                        return {'complete': True, 'network': True, 'cache_hit': False,
                                'destination': destination, 'sha256': expected_sha256, 'bytes': offset}
                    os.remove(part)
                last_error = error
            except (OSError, ValueError, http.client.HTTPException) as error:
                last_error = error
    # Do not expose URL/proxy credentials through exception text.
    raise RuntimeError('Verified download failed after %d bounded attempts (%s)' %
                       (attempts, type(last_error).__name__)) from None
