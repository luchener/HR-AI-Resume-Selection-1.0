"""Build the release SQL images from an immutable git SHA (archive-only inputs)."""
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import tarfile
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SHA = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True).stdout.strip()
SHORT = SHA[:12]
NODE_IMAGE = 'node:24-alpine@sha256:8a32c8cf649a49915a66e81e7a609223c708d0adabf288ee3f4077d765542f85'
MANIFEST = ROOT / 'deploy' / 'release' / ('manifest-%s.json' % SHORT)


def run(args, **kwargs):
    result = subprocess.run(args, cwd=kwargs.pop('cwd', ROOT), capture_output=True, text=True,
                            encoding='utf-8', errors='replace', **kwargs)
    if result.returncode:
        raise SystemExit('command failed: ' + ' '.join(args[:4]) + ' :: ' + (result.stderr or '')[-400:])
    return result


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def archive_tree(paths, destination):
    archive = destination / 'src.tar'
    with archive.open('wb') as handle:
        subprocess.run(['git', 'archive', '--format=tar', SHA, *paths], cwd=ROOT, stdout=handle, check=True)
    with tarfile.open(archive) as tar:
        tar.extractall(destination)
    return archive


def main():
    if run(['git', 'status', '--porcelain', '--untracked-files=no']).stdout.strip():
        raise SystemExit('tracked tree is dirty; commit the recipe first')
    manifest = {'source_commit': SHA, 'node_image': NODE_IMAGE,
                'base_images': {'sql_backend': 'resume-sql-preview-backend@sha256:78f0e5cea386eae0872384b88d8e090d16523e9838916bc2d99ee8f5adb754b6',
                                'node': NODE_IMAGE},
                'inputs': {}}
    backend = pathlib.Path(tempfile.mkdtemp(prefix='release-backend-'))
    archive_tree(['apps/backend'], backend)
    for fixture in (backend / 'apps' / 'backend').glob('test_*.py'):
        fixture.unlink()
    shutil.copy(ROOT / 'deploy' / 'release' / 'Dockerfile.sql-backend', backend / 'Dockerfile')
    files = sorted(p for p in (backend / 'apps' / 'backend').rglob('*') if p.is_file())
    manifest['inputs']['backend_source_sha256'] = hashlib.sha256(
        json.dumps({p.relative_to(backend).as_posix(): sha256(p) for p in files}, sort_keys=True).encode()).hexdigest()
    manifest['inputs']['backend_files'] = len(files)
    manifest['inputs']['requirements_sha256'] = sha256(ROOT / 'apps' / 'backend' / 'requirements.txt')
    manifest['inputs']['requirements_database_sha256'] = sha256(ROOT / 'apps' / 'backend' / 'requirements-database.txt')
    tag_backend = 'resume-release-sql-backend:' + SHORT
    run(['docker', 'build', '--platform', 'linux/amd64',
         '--label', 'org.opencontainers.image.revision=' + SHA,
         '--label', 'resume.protocol=sql', '--label', 'resume.recipe=release-backend',
         '-t', tag_backend, str(backend)], timeout=900)
    manifest['backend'] = {'tag': tag_backend,
                           'image_id': run(['docker', 'image', 'inspect', '--format', '{{.Id}}', tag_backend]).stdout.strip()}

    frontend = pathlib.Path(tempfile.mkdtemp(prefix='release-frontend-'))
    archive_tree(['apps/frontend'], frontend)
    src = frontend / 'apps' / 'frontend'
    cache = pathlib.Path(os.environ.get('LOCALAPPDATA', str(pathlib.Path.home()))) / 'npm-cache'
    build_cmd = ('npm ci --offline --cache /npm-cache --no-audit --no-fund || '
                 'npm ci --prefer-offline --cache /npm-cache --no-audit --no-fund; '
                 'NEXT_PUBLIC_API_URL= NEXT_PUBLIC_ANALYSIS_BACKEND=sql '
                 'BACKEND_INTERNAL_URL=http://backend:8000 NEXT_TELEMETRY_DISABLED=1 '
                 'npm run build && node -e "console.log(process.version)" && npm --version')
    result = run(['docker', 'run', '--rm', '-v', str(src) + ':/src', '-v', str(cache) + ':/npm-cache',
                  '-w', '/src', NODE_IMAGE, 'sh', '-c', build_cmd], timeout=1800)
    versions = result.stdout.strip().splitlines()[-2:]
    manifest['inputs']['package_lock_sha256'] = sha256(src / 'package-lock.json')
    node_modules = sorted(p for p in (src / 'node_modules').rglob('*') if p.is_file())
    manifest['inputs']['node_modules_sha256'] = hashlib.sha256(
        json.dumps({p.relative_to(src).as_posix(): sha256(p) for p in node_modules}, sort_keys=True).encode()).hexdigest()
    manifest['inputs']['node_modules_files'] = len(node_modules)
    manifest['frontend_build'] = {'node': versions[0] if len(versions) > 0 else 'unknown',
                                  'npm': versions[1] if len(versions) > 1 else 'unknown',
                                  'build_environment': 'linux/' + NODE_IMAGE}
    context = pathlib.Path(tempfile.mkdtemp(prefix='release-frontend-image-'))
    for name in ('public', '.next', 'node_modules'):
        shutil.copytree(src / name, context / name)
    for name in ('package.json', 'next.config.ts'):
        shutil.copy(src / name, context / name)
    shutil.copy(ROOT / 'deploy' / 'release' / 'Dockerfile.sql-frontend', context / 'Dockerfile')
    tag_frontend = 'resume-release-sql-frontend:' + SHORT
    run(['docker', 'build', '--platform', 'linux/amd64',
         '--label', 'org.opencontainers.image.revision=' + SHA,
         '--label', 'resume.protocol=sql', '--label', 'resume.recipe=release-frontend',
         '-t', tag_frontend, str(context)], timeout=1800)
    manifest['frontend'] = {'tag': tag_frontend,
                            'image_id': run(['docker', 'image', 'inspect', '--format', '{{.Id}}', tag_frontend]).stdout.strip(),
                            'build_args': {'NEXT_PUBLIC_API_URL': '', 'NEXT_PUBLIC_ANALYSIS_BACKEND': 'sql',
                                           'BACKEND_INTERNAL_URL': 'http://backend:8000'}}
    MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == '__main__':
    main()
