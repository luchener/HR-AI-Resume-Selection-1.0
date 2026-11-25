"""Offline bundle CLI. No transfers, database commands or production cutover.

Use a private staging directory OUTSIDE the repository. Encryption public key
fingerprints and ciphertext checksums are public identifiers. Passwords are
read from a private file, never supplied in arguments or printed. Store private
keys separately from backup ciphertext and production. Restore output is private
but contains decrypted secrets; never stage it in Git or use a shared directory.
"""
import argparse
import json
import os
from pathlib import Path
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'apps/backend'))
from db_recovery_bundle import no_links, pack_bundle, restore_bundle, separate, verify_restored


def external(path):
    path = no_links(path)
    repository = Path(__file__).resolve().parents[1]
    if path == repository or repository in path.parents:
        raise ValueError('Sensitive staging and key files must be outside the repository')
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='action', required=True)
    for action in ('pack', 'restore'):
        cmd = subs.add_parser(action)
        cmd.add_argument('--gpg', type=Path, required=True)
        cmd.add_argument('--recipient', required=True)
        cmd.add_argument('--scratch', type=Path, required=True)
        cmd.add_argument('--output', type=Path, required=True)
        if action == 'pack':
            cmd.add_argument('--source-root', type=Path, required=True)
            cmd.add_argument('--public-key', type=Path, required=True)
            cmd.add_argument('--recovery-point', required=True)
        else:
            cmd.add_argument('--bundle', type=Path, required=True)
            cmd.add_argument('--expected-sha256', required=True)
            cmd.add_argument('--private-key', type=Path, required=True)
            cmd.add_argument('--passphrase-file', type=Path, required=True)
    verify = subs.add_parser('verify')
    verify.add_argument('--root', type=Path, required=True)
    verify.add_argument('--expected-sha256', required=True)
    verify.add_argument('--expected-manifest-sha256', required=True)
    args = parser.parse_args()
    try:
        if args.action == 'verify':
            result = verify_restored(external(args.root), args.expected_sha256, args.expected_manifest_sha256)
        else:
            common = dict(gpg=args.gpg, recipient=args.recipient, scratch=external(args.scratch))
            if args.action == 'pack':
                result = pack_bundle(external(args.source_root), external(args.output),
                                     public_key=external(args.public_key), point=args.recovery_point, **common)
            else:
                password_file = external(args.passphrase_file)
                if (not password_file.is_file() or password_file.stat().st_size > 4096
                        or not separate(password_file, external(args.bundle).parent)
                        or (os.name != 'nt' and stat.S_IMODE(password_file.stat().st_mode) & 0o077)):
                    raise ValueError('Private passphrase file permissions or size invalid')
                result = restore_bundle(external(args.bundle), external(args.output),
                                        expected_sha256=args.expected_sha256,
                                        private_key=external(args.private_key),
                                        passphrase=password_file.read_bytes().rstrip(b'\r\n'), **common)
        print(json.dumps({'action': args.action, 'passed': True, **result}, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({'action': args.action, 'passed': False, 'error_type': type(exc).__name__,
                          'raw_output_suppressed': True, 'production_acceptance': False}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
