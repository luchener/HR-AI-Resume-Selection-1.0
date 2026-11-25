"""Build a code-only, commit-bound SQL preview context; never include local data."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT).decode('utf-8').strip()


def included(name):
    path = Path(name)
    if any(part in {'.git', 'node_modules', '.next', 'data', 'logs', '__pycache__'} for part in path.parts):
        return False
    if path.name.startswith('.env') or path.suffix in {'.pem', '.key', '.log'}:
        return False
    if name.startswith('apps/frontend/'):
        return not path.name.startswith(('test_', 'smoke_test_'))
    if name.startswith('apps/backend/'):
        return (name.startswith(('apps/backend/tools/', 'apps/backend/migrations/'))
                or (path.parent.as_posix() == 'apps/backend' and
                    (path.suffix == '.py' and not path.name.startswith(('test_', 'smoke_test_'))
                     or path.name in {'school_tiers.json', 'requirements.txt', 'requirements-database.txt',
                                      'alembic.ini', 'Dockerfile.sql-preview'})))
    return name in {'.dockerignore', 'docker-compose.sql-preview.yml', 'scripts/database-init.sh',
                    'scripts/sql-preview-cloud.py', 'scripts/build-sql-preview-package.py'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    target = Path(args.output).resolve()
    target.relative_to(ROOT)
    if target.exists():
        raise SystemExit('Refusing to replace an existing preview package')
    names = sorted(name for name in git('ls-files', '-z').split('\0') if name and included(name))
    if git('status', '--porcelain=v1', '--', *names):
        raise SystemExit('Commit the preview sources before packaging')
    commit = git('rev-parse', 'HEAD')
    manifest = {'commit': commit, 'tree': git('rev-parse', 'HEAD^{tree}'), 'files': {}}
    target.parent.mkdir(exist_ok=True, parents=True)
    with target.open('xb') as handle, tarfile.open(fileobj=handle, mode='w:gz') as archive:
        for name in names:
            content = subprocess.check_output(['git', 'show', commit + ':' + name], cwd=ROOT)
            manifest['files'][name] = hashlib.sha256(content).hexdigest()
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(content), 0o644, 0
            archive.addfile(info, io.BytesIO(content))
        content = json.dumps(manifest, sort_keys=True, indent=2).encode() + b'\n'
        info = tarfile.TarInfo('sql-preview-manifest.json')
        info.size, info.mode = len(content), 0o644
        archive.addfile(info, io.BytesIO(content))
    print(json.dumps({'package': str(target), 'commit': commit, 'files': len(names),
                      'bytes': target.stat().st_size, 'sha256': hashlib.sha256(target.read_bytes()).hexdigest()}))


if __name__ == '__main__':
    main()
