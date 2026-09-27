"""
Compare row counts of every table between two databases (backup drill).

    python backup/compare_counts.py SOURCE_URL RESTORED_URL [--require TABLE ...]

Exits non-zero if any table differs, or if a --require table is empty in the
source (which would mean the drill proved nothing).
"""
import argparse
import sys

import psycopg2


def counts(url):
    with psycopg2.connect(url) as conn, conn.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY table_name")
        tables = [r[0] for r in cur.fetchall()]
        result = {}
        for table in tables:
            cur.execute(f'SELECT count(*) FROM "{table}"')
            result[table] = cur.fetchone()[0]
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('source')
    parser.add_argument('restored')
    parser.add_argument('--require', nargs='*', default=[])
    args = parser.parse_args()

    src, dst = counts(args.source), counts(args.restored)
    problems = []
    for table in sorted(set(src) | set(dst)):
        a, b = src.get(table), dst.get(table)
        mark = 'ok' if a == b else 'MISMATCH'
        if a != b:
            problems.append(table)
        print(f'{table:40} {a!s:>8} {b!s:>8}  {mark}')
    for table in args.require:
        if not src.get(table):
            problems.append(f'{table} (empty in source)')
    if problems:
        sys.exit(f'Backup drill failed: {problems}')
    print(f'All {len(src)} tables match.')


if __name__ == '__main__':
    main()
