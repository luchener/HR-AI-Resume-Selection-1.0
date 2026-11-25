"""Build the committed frontend using integrity-verified public npm tarballs.

Only a derived build recipe and exact lockfile contents are added to the context.
The application sources and dependency versions remain those in the code package.
No npm configuration, credentials, or unrelated host cache metadata are copied.
"""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
from time import sleep
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def download(address):
    # npm's CDN supports byte ranges. Bound individual reads so a slow large
    # native archive cannot hold the entire build on one fragile connection.
    request = urllib.request.Request(address, headers={'Range': 'bytes=0-0', 'Accept-Encoding': 'identity'})
    with urllib.request.urlopen(request, timeout=30) as response:
        match = re.fullmatch(r'bytes 0-0/(\d+)', response.headers.get('Content-Range', ''))
        if response.status != 206 or not match:
            data = response.read(128 * 1024 * 1024 + 1)
            return data
        size = int(match[1])
        response.read(1)
    if not 0 < size <= 128 * 1024 * 1024:
        raise ValueError('Locked dependency exceeds archive size limit')
    chunk_size = 1024 * 1024

    def chunk(start):
        end = min(start + chunk_size, size) - 1
        for attempt in range(4):
            try:
                request = urllib.request.Request(address, headers={'Range': f'bytes={start}-{end}', 'Accept-Encoding': 'identity'})
                with urllib.request.urlopen(request, timeout=30) as response:
                    if response.status != 206 or response.headers.get('Content-Range') != f'bytes {start}-{end}/{size}':
                        raise OSError('Unexpected public archive byte range')
                    data = response.read(end - start + 2)
                if len(data) != end - start + 1:
                    raise OSError('Public archive byte range was incomplete')
                return data
            except (OSError, http.client.HTTPException):
                if attempt == 3:
                    raise
                sleep(attempt + 1)
    with ThreadPoolExecutor(max_workers=4) as pool:
        return b''.join(pool.map(chunk, range(0, size, chunk_size)))


def selected(package):
    for key, target in [('os', 'linux'), ('cpu', 'x64')]:
        values = package.get(key, [])
        if '!' + target in values or (values and target not in values and not all(v.startswith('!') for v in values)):
            return False
    return bool(package.get('integrity'))


def content_path(cache, integrity):
    choices = [value.split('-', 1) for value in integrity.split()]
    algorithm, encoded = next((x for x in choices if x[0] == 'sha512'), choices[0])
    if algorithm not in {'sha512', 'sha1'}:
        raise ValueError('Unsupported locked integrity')
    digest = base64.b64decode(encoded, validate=True).hex()
    return cache / 'content-v2' / algorithm / digest[:2] / digest[2:4] / digest[4:], algorithm, digest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    if args.evidence.exists():
        raise SystemExit('Use a new evidence file')
    with tarfile.open(args.package) as source:
        manifest = json.load(source.extractfile('sql-preview-manifest.json'))
        lock_bytes = source.extractfile('apps/frontend/package-lock.json').read()
        recipe = source.extractfile('apps/frontend/Dockerfile').read()
    commit = manifest['commit']
    cache = ROOT / '.test-tmp' / 'sql-preview-public-npm-cache'
    host_cache = Path(os.environ['LOCALAPPDATA']) / 'npm-cache' / '_cacache'
    packages = [value for value in json.loads(lock_bytes)['packages'].values() if selected(value)]

    def acquire(package):
        destination, algorithm, digest = content_path(cache, package['integrity'])
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_file() and hashlib.new(algorithm, destination.read_bytes()).hexdigest() == digest:
            return destination
        host, _, _ = content_path(host_cache, package['integrity'])
        if host.is_file() and hashlib.new(algorithm, host.read_bytes()).hexdigest() == digest:
            shutil.copyfile(host, destination)
            return destination
        url = urllib.parse.urlsplit(package['resolved'])
        if url.scheme != 'https' or url.hostname not in {'registry.npmjs.org', 'registry.npmmirror.com'}:
            raise ValueError('Only public locked npm archives accepted')
        address = urllib.parse.urlunsplit(('https', 'registry.npmmirror.com', url.path, '', ''))
        for attempt in range(4):
            try:
                data = download(address)
                if len(data) > 128 * 1024 * 1024:
                    raise ValueError('Locked dependency exceeds archive size limit')
                if hashlib.new(algorithm, data).hexdigest() != digest:
                    raise OSError('Locked dependency download incomplete or integrity mismatch: ' + address)
                destination.write_bytes(data)
                return destination
            except (OSError, TimeoutError, http.client.HTTPException):
                if attempt == 3:
                    raise
                sleep(attempt + 1)

    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = sorted(set(pool.map(acquire, packages)))
    print(json.dumps({'verified_linux_dependencies': len(packages), 'unique_archives': len(paths)}), flush=True)
    derived = recipe.replace(b'RUN npm ci', b'COPY .sql-preview-npm-cache/ /root/.npm/_cacache/\nRUN npm ci --offline --no-audit --no-fund', 1)
    if derived == recipe:
        raise ValueError('Expected frontend dependency stage missing')
    context = ROOT / '.test-tmp' / ('sql-preview-offline-context-' + commit[:12] + '.tar.gz')
    if context.exists():
        raise SystemExit('Existing offline context refused')
    with context.open('xb') as output, tarfile.open(fileobj=output, mode='w:gz') as target:
        with tarfile.open(args.package) as source:
            for member in source.getmembers():
                target.addfile(member, source.extractfile(member))
        for path in paths:
            info = tarfile.TarInfo('.sql-preview-npm-cache/' + path.relative_to(cache).as_posix())
            info.size, info.mode = path.stat().st_size, 0o644
            with path.open('rb') as handle:
                target.addfile(info, handle)
        info = tarfile.TarInfo('Dockerfile.frontend-offline')
        info.size, info.mode = len(derived), 0o644
        target.addfile(info, io.BytesIO(derived))
    logpath = ROOT / '.test-tmp' / ('sql-preview-frontend-offline-' + commit[:12] + '.log')
    with context.open('rb') as handle, logpath.open('wb') as log:
        result = subprocess.run(['docker', 'build', '--platform', 'linux/amd64',
            '--label', 'org.opencontainers.image.revision=' + commit,
            '-f', 'Dockerfile.frontend-offline', '-t', 'resume-sql-preview-frontend:' + commit[:12],
            '--build-arg', 'NEXT_PUBLIC_API_URL=', '--build-arg', 'NEXT_PUBLIC_ANALYSIS_BACKEND=sql',
            '--build-arg', 'BACKEND_INTERNAL_URL=http://backend:8000', '-'],
            stdin=handle, stdout=log, stderr=log, timeout=900)
    evidence = {'passed': result.returncode == 0, 'code_commit': commit,
        'code_package_sha256': hashlib.sha256(args.package.read_bytes()).hexdigest(),
        'lock_sha256': hashlib.sha256(lock_bytes).hexdigest(),
        'derived_recipe_sha256': hashlib.sha256(derived).hexdigest(),
        'build_helper_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'dependency_install': 'npm ci --offline --no-audit --no-fund',
        'dependency_download_host': 'registry.npmmirror.com',
        'verified_packages': len(packages), 'unique_archives': len(paths),
        'build_log': str(logpath.relative_to(ROOT)), 'production_cutover': False}
    with args.evidence.open('x') as output:
        json.dump(evidence, output, indent=2)
    print(json.dumps(evidence))
    if result.returncode:
        print(logpath.read_text(encoding='utf-8', errors='replace')[-2500:])
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
