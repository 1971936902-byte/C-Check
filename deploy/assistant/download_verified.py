"""Download a pinned artifact in bounded ranges and verify its complete SHA-256."""
import argparse
from contextlib import contextmanager
import hashlib
from http.client import parse_headers
from io import BytesIO
import os
from pathlib import Path
import re
import tempfile
import time
import subprocess
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


def digest(path, algorithm='sha256'):
    hasher = hashlib.new(algorithm)
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            hasher.update(chunk)
    return hasher.hexdigest()


@contextmanager
def curlopen(request, timeout):
    with tempfile.TemporaryDirectory(prefix='ccheck-range-') as temporary:
        headers, body = Path(temporary) / 'headers', Path(temporary) / 'body'
        command = ['curl', '--fail', '--silent', '--show-error', '--location',
                   '--proto', '=https', '--proto-redir', '=https', '--connect-timeout', '10',
                   '--max-time', str(timeout), '--dump-header', str(headers), '--output', str(body)]
        for key, value in request.header_items():
            command += ['--header', key + ': ' + value]
        command.append(request.full_url)
        result = subprocess.run(command, capture_output=True, timeout=timeout + 5)
        if result.returncode:
            raise OSError(f'curl transfer failed with exit code {result.returncode}')
        # curl may include a proxy CONNECT response and redirect headers.
        blocks = [block for block in headers.read_bytes().split(b'\r\n\r\n') if block.startswith(b'HTTP/')]
        if not blocks:
            raise ValueError('missing HTTP response headers')
        status_line, _, fields = blocks[-1].partition(b'\r\n')
        status = int(status_line.split()[1])
        parsed = parse_headers(BytesIO(fields + b'\r\n\r\n'))
        with body.open('rb') as source:
            yield SimpleNamespace(status=status, headers=parsed, read=source.read)


def download(url, expected, destination, *, chunk_size=1024 * 1024, max_size=256 * 1024 * 1024,
             transport='urllib', algorithm='sha256'):
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.username or parts.password or parts.fragment:
        raise ValueError('artifact URL must be HTTPS without credentials or a fragment')
    if algorithm not in {'sha256', 'sha512'} or not re.fullmatch(
            '[0-9a-f]{' + str(64 if algorithm == 'sha256' else 128) + '}', expected):
        raise ValueError('a pinned SHA-256 or SHA-512 is required')
    if not 1 <= chunk_size <= max_size:
        raise ValueError('invalid chunk size')
    if transport not in {'urllib', 'curl'}:
        raise ValueError('invalid transport')
    opener = curlopen if transport == 'curl' else urlopen
    destination = Path(destination)
    if destination.is_symlink():
        raise ValueError('refusing symbolic-link destination')
    if destination.exists():
        if digest(destination, algorithm) != expected:
            raise ValueError('existing artifact does not match; it was preserved')
        return destination
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.artifact-', delete=False) as output:
        temporary = Path(output.name)
        try:
            offset, total = 0, None
            hasher = hashlib.new(algorithm)
            while total is None or offset < total:
                end = min(offset + chunk_size, total or max_size) - 1
                query = urlencode(parse_qsl(parts.query) + [('ccheck_chunk', str(offset))])
                target = urlunsplit(parts._replace(query=query))
                request = Request(target, headers={'Range': f'bytes={offset}-{end}',
                    'Accept-Encoding': 'identity', 'User-Agent': 'CCheck-Artifact-Downloader/1.0'})
                for attempt in range(3):
                    try:
                        with opener(request, timeout=30) as response:
                            match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
                            if response.status != 206 or not match:
                                raise ValueError('server did not honor the range request')
                            first, last, size = map(int, match.groups())
                            if (size > max_size or size <= 0 or first != offset
                                    or last != min(end, size - 1) or (total is not None and total != size)):
                                raise ValueError('inconsistent artifact range')
                            data = response.read(chunk_size + 1)
                            if len(data) != last - first + 1:
                                raise ValueError('incomplete artifact range')
                        break
                    except (OSError, ValueError, subprocess.TimeoutExpired):
                        if attempt == 2:
                            raise
                        time.sleep(attempt + 1)
                total = size
                output.write(data)
                hasher.update(data)
                offset += len(data)
                print(f'Artifact bytes: {offset}/{total}', flush=True)
            if hasher.hexdigest() != expected:
                raise ValueError('artifact ' + algorithm.upper() + ' mismatch')
            output.close()
            # Exclusive creation prevents replacing a file created during download.
            os.link(temporary, destination)
        finally:
            output.close()
            temporary.unlink(missing_ok=True)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    hashes = parser.add_mutually_exclusive_group(required=True)
    hashes.add_argument('--sha256')
    hashes.add_argument('--sha512')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--chunk-size', type=int, default=1024 * 1024)
    parser.add_argument('--transport', choices=['urllib', 'curl'], default='urllib')
    args = parser.parse_args()
    algorithm = 'sha256' if args.sha256 else 'sha512'
    download(args.url, args.sha256 or args.sha512, args.output, chunk_size=args.chunk_size,
             transport=args.transport, algorithm=algorithm)
    print('ARTIFACT_' + algorithm.upper() + '_VERIFIED')


if __name__ == '__main__':
    main()
