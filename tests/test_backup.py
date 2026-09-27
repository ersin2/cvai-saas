"""
The backup scripts in backup/. The full dump → encrypt → restore round trip
runs against real PostgreSQL in CI (the backup-drill job); these cover the
parts that decide what is kept, and what could leak into a public log.
"""

import datetime
import os
import shutil
import subprocess
import sys
import tempfile
from unittest import skipUnless
from unittest.mock import patch

from django.test import SimpleTestCase

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'backup'))
import backup_db  # noqa: E402


class _FakeS3:
    def __init__(self, keys):
        self.keys = set(keys)

    def get_paginator(self, name):
        s3 = self

        class _P:
            def paginate(self, Bucket, Prefix):
                return [{'Contents': [{'Key': k} for k in sorted(s3.keys) if k.startswith(Prefix)]}]
        return _P()

    def delete_object(self, Bucket, Key):
        self.keys.remove(Key)


def _key(day):
    return f'cvai/{day:%Y/%m}/cvai-{day:%Y%m%d}-031700.dump.gpg'


class PruneTest(SimpleTestCase):

    def test_keeps_five_weeks_of_dailies_then_monthlies_for_a_year(self):
        today = datetime.date(2026, 9, 27)
        days = [today - datetime.timedelta(days=n) for n in range(0, 500)]
        s3 = _FakeS3([_key(d) for d in days] + ['cvai/notes.txt'])

        backup_db.prune(s3, 'bucket', today)

        kept = {datetime.datetime.strptime(backup_db.KEY_RE.search(k).group(1), '%Y%m%d').date()
                for k in s3.keys if backup_db.KEY_RE.search(k)}
        for n in range(0, 36):
            self.assertIn(today - datetime.timedelta(days=n), kept, 'every backup of the last 35 days')
        older = {d for d in kept if (today - d).days > 35}
        self.assertTrue(older, 'monthlies survive')
        self.assertTrue(all(d.day == 1 for d in older), 'only the 1st of each month beyond five weeks')
        self.assertTrue(all((today - d).days <= 400 for d in kept), 'nothing past ~13 months')
        self.assertIn('cvai/notes.txt', s3.keys, 'files that are not backups are never touched')


class NoSecretsInLogsTest(SimpleTestCase):

    def test_a_failing_tool_does_not_echo_its_arguments(self):
        url = 'postgres://user:hunter2@db.example.com/cvai'
        failure = subprocess.CalledProcessError(1, ['pg_dump', '--dbname', url])
        with patch('backup_db.subprocess.run', side_effect=failure), self.assertRaises(SystemExit) as ctx:
            backup_db.run(['/usr/lib/postgresql/16/bin/pg_dump', '--dbname', url])
        self.assertNotIn('hunter2', str(ctx.exception.code))
        self.assertIn('pg_dump failed with exit code 1', str(ctx.exception.code))

    def test_missing_setting_names_it(self):
        with patch.dict(os.environ, {'BACKUP_PASSPHRASE': ''}), self.assertRaises(SystemExit) as ctx:
            backup_db.env('BACKUP_PASSPHRASE')
        self.assertEqual(ctx.exception.code, 'BACKUP_PASSPHRASE is not set')


@skipUnless(shutil.which('gpg'), 'gpg not installed')
class EncryptionTest(SimpleTestCase):

    def test_ciphertext_hides_the_data_and_needs_the_passphrase(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, sealed, out = (os.path.join(tmp, n) for n in ('plain', 'sealed', 'out'))
            with open(src, 'wb') as f:
                f.write(b'alice@example.com resume text ' * 100)
            backup_db.encrypt(src, sealed, 'correct horse')
            with open(sealed, 'rb') as f:
                self.assertNotIn(b'alice@example.com', f.read())

            def decrypt(passphrase):
                return subprocess.run(['gpg', '--batch', '--yes', '--quiet', '--pinentry-mode', 'loopback',
                                       '--passphrase-fd', '0', '--decrypt', '--output', out, sealed],
                                      input=passphrase.encode(), capture_output=True).returncode

            self.assertNotEqual(decrypt('wrong'), 0)
            self.assertEqual(decrypt('correct horse'), 0)
            with open(out, 'rb') as f:
                self.assertIn(b'alice@example.com', f.read())
