#!/usr/bin/env bash
# Render build script — runs before the web service starts
set -o errexit

pip install -r requirements.txt
python manage.py collectstatic --no-input

# Migrations: the internal DB hostname is NOT resolvable during build on
# Render's free tier — only at runtime.  We attempt here but don't fail
# the build if the DB isn't reachable yet.
python manage.py migrate --no-input || echo "⚠️  migrate skipped (DB not reachable during build — will run at startup)"