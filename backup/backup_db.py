"""
Encrypted backup of the production database. Runs nightly from
.github/workflows/backup.yml; see BACKUPS.md.

    PROD_DATABASE_URL    postgres://… (Render: the database's External URL)
    BACKUP_PASSPHRASE    encrypts the dump; without it the backup is unreadable
    BACKUP_S3_ENDPOINT   S3-compatible endpoint (Supabase Storage → S3 connection)
    BACKUP_S3_REGION     e.g. eu-central-1
    BACKUP_S3_BUCKET     a PRIVATE bucket, e.g. cvai-backups
    AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY   S3 keys for that bucket

The repository is public, so the dump never touches a build artifact, a log or
the repo: it is encrypted on the runner and only the ciphertext leaves it.
Neither the database URL nor the passphrase is ever printed.

`--output PATH` writes the encrypted dump locally and skips the upload.
"""
import argparse
import datetime
import os
import re
import subprocess
import sys
import tempfile

KEY_RE = re.compile(r'cvai-(\d{8})-\d{6}\.dump\.gpg$')
PREFIX = 'cvai/'
KEEP_DAILY_DAYS = 35      # every backup for five weeks
KEEP_MONTHLY_DAYS = 400   # then the one from the 1st of each month, for a year


def env(name):
    value = os.environ.get(name, '')
    if not value:
        sys.exit(f'{name} is not set')
    return value


def run(args, **kwargs):
    """subprocess.run that never echoes its arguments: they include the database URL."""
    try:
        subprocess.run(args, check=True, **kwargs)
    except subprocess.CalledProcessError as exc:
        sys.exit(f'{os.path.basename(args[0])} failed with exit code {exc.returncode}')


def server_major(url):
    import psycopg2
    with psycopg2.connect(url) as conn, conn.cursor() as cur:
        cur.execute('SHOW server_version_num')
        return int(cur.fetchone()[0]) // 10000


def pg_tool(name, major):
    """The client tool matching the server's major version (pg_dump refuses newer servers)."""
    path = f'/usr/lib/postgresql/{major}/bin/{name}'
    return path if os.path.exists(path) else name


def dump(url, out_path):
    major = server_major(url)
    print(f'PostgreSQL server {major}; dumping with {pg_tool("pg_dump", major)}')
    run([pg_tool('pg_dump', major), '--format=custom', '--no-owner', '--no-privileges',
         '--file', out_path, '--dbname', url])


def encrypt(src, dst, passphrase):
    run(['gpg', '--batch', '--yes', '--quiet', '--pinentry-mode', 'loopback',
         '--symmetric', '--cipher-algo', 'AES256', '--passphrase-fd', '0',
         '--output', dst, src], input=passphrase.encode())


def s3_client():
    import boto3
    from botocore.config import Config
    return boto3.client('s3', endpoint_url=env('BACKUP_S3_ENDPOINT'),
                        region_name=os.environ.get('BACKUP_S3_REGION') or 'us-east-1',
                        config=Config(s3={'addressing_style': 'path'}))


def backup_keys(s3, bucket):
    keys = []
    for page in s3.get_paginator('list_objects_v2').paginate(Bucket=bucket, Prefix=PREFIX):
        keys += [o['Key'] for o in page.get('Contents', [])]
    return sorted(k for k in keys if KEY_RE.search(k))


def prune(s3, bucket, today):
    """Delete dailies older than KEEP_DAILY_DAYS, except the 1st of the month; monthlies after a year."""
    removed = 0
    for key in backup_keys(s3, bucket):
        day = datetime.datetime.strptime(KEY_RE.search(key).group(1), '%Y%m%d').date()
        age = (today - day).days
        if age > KEEP_MONTHLY_DAYS or (age > KEEP_DAILY_DAYS and day.day != 1):
            s3.delete_object(Bucket=bucket, Key=key)
            removed += 1
    return removed


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument('--output', help='write the encrypted dump here and skip the upload')
    args = parser.parse_args()

    url = env('PROD_DATABASE_URL')
    passphrase = env('BACKUP_PASSPHRASE')
    now = datetime.datetime.now(datetime.timezone.utc)

    with tempfile.TemporaryDirectory() as tmp:
        plain = os.path.join(tmp, 'cvai.dump')
        dump(url, plain)
        sealed = args.output or os.path.join(tmp, 'cvai.dump.gpg')
        encrypt(plain, sealed, passphrase)
        os.remove(plain)
        size = os.path.getsize(sealed)
        print(f'Encrypted dump: {size:,} bytes')
        if size < 1024:
            sys.exit('Dump is implausibly small — refusing to treat it as a backup')
        if args.output:
            return

        bucket = env('BACKUP_S3_BUCKET')
        key = f'{PREFIX}{now:%Y/%m}/cvai-{now:%Y%m%d-%H%M%S}.dump.gpg'
        s3 = s3_client()
        s3.upload_file(sealed, bucket, key)
        stored = s3.head_object(Bucket=bucket, Key=key)['ContentLength']
        if stored != size:
            sys.exit(f'Upload size mismatch: {stored} != {size}')
        print(f'Uploaded {key} ({stored:,} bytes)')
        print(f'Pruned {prune(s3, bucket, now.date())} old backup(s); '
              f'{len(backup_keys(s3, bucket))} kept')


if __name__ == '__main__':
    main()
