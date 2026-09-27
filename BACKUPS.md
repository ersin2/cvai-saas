# Database backups

Production data (accounts, subscriptions, generations, the job tracker) lives in
one Render PostgreSQL database. `render.yaml` declares it `plan: free`, and a
free Render database **expires 30 days after it was created and is deleted
14 days after that**, with no backups of its own. Check its expiry date in the
Render dashboard: Databases → the CVAI database → Info.

Two things protect the data:

1. **A nightly encrypted backup** (`.github/workflows/backup.yml`) copies the
   database to a private storage bucket at 03:17 UTC every day.
2. **A restore drill** (the `backup-drill` job in `.github/workflows/tests.yml`)
   runs on every push. It backs up a seeded test database, restores it into a
   fresh one and checks that every table has the same number of rows. If the
   backup scripts break, CI goes red long before a restore is needed.

## How the backup works

`backup/backup_db.py` runs `pg_dump` with the client version that matches the
server. It encrypts the dump with GnuPG (AES-256, using your passphrase) and
uploads only the encrypted file to the bucket. It then prunes old backups:

- every backup from the last 35 days is kept;
- after that, only the backup from the 1st of each month is kept;
- monthly backups are deleted after about 13 months.

The repository is public, so no backup is ever a GitHub build artifact: anyone
on GitHub can download those. The unencrypted dump exists only on the runner.
Errors never print the database URL.

## One-time setup (about 10 minutes)

Until this is done, the nightly job skips with a notice and does not fail.

1. **Create the bucket.** In Supabase, open Storage and create a bucket
   named `cvai-backups`. Leave **Public bucket** off.
2. **Create S3 keys.** Go to Storage → Settings → S3 Connection. Copy the
   **Endpoint** and **Region**, then choose **New access key**.
3. **Choose a passphrase.** Use a long random one, for example from
   `python -c "import secrets; print(secrets.token_urlsafe(32))"`. **Save it
   in your password manager.** Without the passphrase every backup is
   unreadable, and it cannot be recovered.
4. **Copy the database URL.** In Render, open the database and copy the
   **External Database URL**.
5. **Add the repository secrets.** In GitHub, open Settings → Secrets and
   variables → Actions → New repository secret, and add:

   | Secret | Value |
   |---|---|
   | `PROD_DATABASE_URL` | Render's External Database URL |
   | `BACKUP_PASSPHRASE` | the passphrase from step 3 |
   | `BACKUP_S3_ENDPOINT` | e.g. `https://<project>.supabase.co/storage/v1/s3` |
   | `BACKUP_S3_REGION` | e.g. `eu-central-1` |
   | `BACKUP_S3_BUCKET` | `cvai-backups` |
   | `BACKUP_S3_ACCESS_KEY_ID` | access key ID from step 2 |
   | `BACKUP_S3_SECRET_ACCESS_KEY` | secret access key from step 2 |

6. **Run it once by hand.** Open Actions → backup → Run workflow. The log
   should end with `Uploaded cvai/… (N bytes)`, and the file should appear in
   the bucket.

## Restoring

Always restore into a **new, empty** database, never over the live one. Check
the restored copy, then point the app at it.

1. Create an empty PostgreSQL database, for example a new Render or Supabase
   database. Copy its connection URL.
2. On a machine with Python, GnuPG and the PostgreSQL client tools (the same
   major version as the new server or newer), set these environment variables:
   `BACKUP_PASSPHRASE`, `BACKUP_S3_ENDPOINT`, `BACKUP_S3_REGION`,
   `BACKUP_S3_BUCKET`, `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`. Then run:

   ```bash
   pip install psycopg2-binary boto3
   python backup/restore_db.py --latest --target "postgres://…/newdb"
   ```

   To restore a specific day, download its `.dump.gpg` from the bucket and use
   `--input that-file.dump.gpg` instead of `--latest`.
3. Open the new database and check it: the number of users, and the most
   recent generation.
4. Point the app at it. `render.yaml` sets `DATABASE_URL` from the `mysitedb`
   database, so edit it there: replace the `fromDatabase:` entry with
   `sync: false`, then set the value by hand in the Render dashboard. If you
   only change it in the dashboard, the next Blueprint sync puts the old value
   back. Migrations run on start and find nothing to apply.

## Before the free database expires

Backups make sure you can recover the data. They do not keep the site running
when the database is deleted. Choose one of these before the expiry date:

- **Upgrade the Render database** to a paid instance type (from about
  $6/month; check Render's current pricing). The data stays where it is and
  nothing else changes. This is the simplest option.
- **Move to Supabase Postgres**, which you already have an account for.
  Supabase's free tier does not expire, but free projects pause after a week
  without activity. To move:
  1. run the backup once by hand;
  2. restore it into Supabase using the steps above (use Supabase's
     **session pooler** connection string, port 5432);
  3. switch `DATABASE_URL` as in step 4 of Restoring;
  4. remove the `databases:` block from `render.yaml` once the site runs on
     Supabase.

After switching, update the `PROD_DATABASE_URL` secret, so the nightly backup
follows the data.
