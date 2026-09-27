"""
Restore an encrypted backup into an EMPTY database. See BACKUPS.md.

    python backup/restore_db.py --latest --target postgres://…/newdb
    python backup/restore_db.py --input cvai-20260927-031700.dump.gpg --target postgres://…/newdb

Needs BACKUP_PASSPHRASE, and for --latest the BACKUP_S3_* / AWS_* variables
that backup_db.py uses. --create makes the target database first if the server
lets you. Restoring over a live database is deliberately not supported: point
the app at the restored copy once it checks out.
"""
import argparse
import os
import sys
import tempfile
from urllib.parse import urlparse, urlunparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backup_db import backup_keys, env, pg_tool, run, s3_client, server_major  # noqa: E402


def create_database(target):
    import psycopg2
    parts = urlparse(target)
    name = parts.path.lstrip('/')
    admin = urlunparse(parts._replace(path='/postgres'))
    conn = psycopg2.connect(admin)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute('SELECT 1 FROM pg_database WHERE datname = %s', [name])
        if not cur.fetchone():
            cur.execute(f'CREATE DATABASE "{name}"')
            print(f'Created database {name}')
    conn.close()


def main():
    parser = argparse.ArgumentParser(description='Restore an encrypted CVAI backup.')
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--input', help='a local .dump.gpg file')
    source.add_argument('--latest', action='store_true', help='download the newest backup from the bucket')
    parser.add_argument('--target', required=True, help='URL of an empty database to restore into')
    parser.add_argument('--create', action='store_true', help='create the target database if missing')
    args = parser.parse_args()
    passphrase = env('BACKUP_PASSPHRASE')

    with tempfile.TemporaryDirectory() as tmp:
        sealed = args.input
        if args.latest:
            s3, bucket = s3_client(), env('BACKUP_S3_BUCKET')
            keys = backup_keys(s3, bucket)
            if not keys:
                sys.exit('No backups in the bucket')
            sealed = os.path.join(tmp, 'latest.dump.gpg')
            s3.download_file(bucket, keys[-1], sealed)
            print(f'Downloaded {keys[-1]}')

        plain = os.path.join(tmp, 'cvai.dump')
        run(['gpg', '--batch', '--yes', '--quiet', '--pinentry-mode', 'loopback',
             '--passphrase-fd', '0', '--decrypt', '--output', plain, sealed], input=passphrase.encode())
        if args.create:
            create_database(args.target)
        major = server_major(args.target)
        run([pg_tool('pg_restore', major), '--no-owner', '--no-privileges', '--exit-on-error',
             '--dbname', args.target, plain])
        print('Restore complete')


if __name__ == '__main__':
    main()
