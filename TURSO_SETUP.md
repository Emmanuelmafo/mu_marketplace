# MU Marketplace — Turso setup

This update keeps SQLite for local development and uses Turso (libSQL) when
`TURSO_DATABASE_URL` is configured. The local `instance/marketplace.db` is not
modified by the application while Turso variables are absent.

## Required Vercel environment variables

Add these two variables to Vercel (Production, Preview, and Development as needed):

- `TURSO_DATABASE_URL` — Turso database URL, usually beginning with `libsql://`
- `TURSO_AUTH_TOKEN` — database authentication token

Do not put credentials in source code or commit them to GitHub.

## One-time admin migration

1. Create the Turso database and generate an authentication token.
2. On the computer that has your existing `instance/marketplace.db`, open a
   terminal in the project folder.
3. Set `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` in that terminal session.
4. Install dependencies: `python -m pip install -r requirements.txt`
5. Run: `python migrate_admin_to_turso.py`

The script initializes the schema and copies only the first active admin
account, preserving its ID and password hash. It does not copy buyers,
sellers, products, orders, payments, messages, or test activity. It refuses
to overwrite an admin account already present in Turso.

## Important deployment notes

- Do not deploy until the Turso database is created and the admin migration
  has completed successfully.
- Remove the old Neon `DATABASE_URL` variable from Vercel after confirming
  Turso is configured; this code does not use Neon.
- Product and profile image uploads still need persistent object storage.
  The current local upload folders are not yet converted to Vercel Blob.
- The original local SQLite database remains the development/test database.
- On Vercel, the app intentionally refuses to fall back to a temporary local
  SQLite file if Turso variables are missing.
