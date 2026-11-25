"""Build the release frontend from archived source + integrity-verified offline cache."""
import base64
import hashlib
import json
import pathlib
import shutil
import subprocess
import tarfile
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SHA = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True).stdout.strip()
SHORT = SHA[:12]
CACHE = ROOT / '.test-tmp' / 'sql-preview-public-npm-cache'
NODE = 'node:24-alpine@sha256:8a32c8cf649a49915a66e81e7a609223c708d0adabf288ee3f4077d765542f85'


def selected(package):
    for key, target in (('os', 'linux'), ('cpu', 'x64')):
        values = package.get(key, [])
        if '!' + target in values or (values and target not in values and not all(v.startswith('!') for v in values)):
            return False
    return bool(package.get('integrity'))


def content_path(cache, integrity):
    choices = [value.split('-', 1) for value in integrity.split()]
    algorithm, encoded = next((x for x in choices if x[0] == 'sha512'), choices[0])
    if algorithm not in {'sha512', 'sha1'}:
        raise SystemExit('unsupported integrity algorithm')
    digest = base64.b64decode(encoded, validate=True).hex()
    return cache / 'content-v2' / algorithm / digest[:2] / digest[2:4] / digest[4:], algorithm, digest


def main():
    if subprocess.run(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=ROOT,
                      capture_output=True, text=True).stdout.strip():
        raise SystemExit('tracked tree is dirty; commit the recipe first')
    if not CACHE.is_dir():
        raise SystemExit('offline cache missing')
    base = CACHE / '_cacache' if (CACHE / '_cacache').is_dir() else CACHE
    lock = json.loads((ROOT / 'apps' / 'frontend' / 'package-lock.json').read_text())
    packages = [value for value in lock['packages'].values() if selected(value)]
    context = pathlib.Path(tempfile.mkdtemp(prefix='release-fe-'))
    archive = context / 'src.tar'
    subprocess.run(['git', 'archive', '--format=tar', '--output', str(archive), SHA, 'apps/frontend'],
                   cwd=ROOT, check=True)
    with tarfile.open(archive) as tar:
        tar.extractall(context)
    src = context / 'build'
    shutil.move(str(context / 'apps' / 'frontend'), str(src))
    shutil.rmtree(context / 'apps')
    shutil.copy(ROOT / 'deploy' / 'release' / 'Dockerfile.sql-frontend', context / 'Dockerfile')
    (context / '.dockerignore').write_text('src.tar\n')
    cache_out = context / 'npm-cache' / '_cacache'
    copied = 0
    total = 0
    for package in packages:
        source, algorithm, digest = content_path(base, package['integrity'])
        if not source.is_file():
            raise SystemExit('missing cached archive for integrity ' + digest[:12])
        data = source.read_bytes()
        if hashlib.new(algorithm, data).hexdigest() != digest:
            raise SystemExit('cache integrity mismatch ' + digest[:12])
        destination, _, _ = content_path(cache_out, package['integrity'])
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        copied += 1
        total += len(data)
    tag = 'resume-release-sql-frontend:' + SHORT
    build = subprocess.run(['docker', 'build', '--platform', 'linux/amd64',
                            '--label', 'org.opencontainers.image.revision=' + SHA,
                            '--label', 'resume.protocol=sql', '--label', 'resume.recipe=release-frontend',
                            '--build-arg', 'NEXT_PUBLIC_API_URL=',
                            '--build-arg', 'NEXT_PUBLIC_ANALYSIS_BACKEND=sql',
                            '--build-arg', 'BACKEND_INTERNAL_URL=http://backend:8000',
                            '-t', tag, str(context)], capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=3600)
    if build.returncode:
        print(json.dumps({'passed': False, 'exit': build.returncode,
                          'error_tail': (build.stderr or '')[-1500:]}, ensure_ascii=False))
        return 2
    image_id = subprocess.run(['docker', 'image', 'inspect', '--format', '{{.Id}}', tag],
                              capture_output=True, text=True).stdout.strip()
    manifest = json.loads(subprocess.run(['docker', 'run', '--rm', '--entrypoint', 'cat', tag,
                                          '/app/release-manifest.json'],
                                         capture_output=True, text=True).stdout)
    result = {'source_commit': SHA, 'node_image': NODE, 'image': tag, 'image_id': image_id,
              'packages': len(packages), 'archives_copied': copied, 'cache_bytes': total,
              'cache_sha256': hashlib.sha256(json.dumps(
                  sorted(p['integrity'] for p in packages)).encode()).hexdigest(),
              'lock_sha256': hashlib.sha256((ROOT / 'apps' / 'frontend' / 'package-lock.json').read_bytes()).hexdigest(),
              'linux_manifest': manifest}
    (ROOT / 'deploy' / 'release' / ('frontend-%s.json' % SHORT)).write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
