"""Extract private source snapshot safely and run read-only migration preflight.

Only aggregates/error types go to stdout; no raw source is printed. Destination
is a dedicated ignored folder, never an application data directory.
"""
from pathlib import Path
import sys
import tarfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'apps'/'backend'))
from db_migrate_json import main

if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]/'apps'/'backend'/'.test-data'/'source-preflight'
    destination = root/'snapshot'
    if destination.exists():
        raise SystemExit('Snapshot destination exists; refusing overwrite')
    with tarfile.open(root/'source.tar.gz','r:gz') as archive:
        members = archive.getmembers()
        if len(members)>100000 or sum(member.size for member in members)>256*1024*1024:
            raise SystemExit('Snapshot exceeds inspection budget')
        for member in members:
            target = (destination/member.name).resolve()
            if not target.is_relative_to(destination.resolve()) or not (member.isfile() or member.isdir()):
                raise SystemExit('Unsafe archive member')
        destination.mkdir()
        archive.extractall(destination,filter='data')
    sys.exit(main([str(destination)]))
