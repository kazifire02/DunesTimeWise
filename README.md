# DunesTimeWise

Flask app for student late arrivals with MySQL storage.

## Configuration

Install Flask, mysql-connector-python, and openpyxl in your Python environment.
Keep your existing `latetrack` database and tables.

Set these environment variables on the server before starting Flask:

| Variable | Value |
| --- | --- |
| `LATETRACK_SECRET_KEY` | Your existing private Flask session key. |
| `LATETRACK_DB_PASSWORD` | Your existing MySQL password. |
| `TIMEWISE_ADMIN_PASSWORD` | The new admin password you want to apply once. |

Store these values in your hosting provider's environment settings or your
local process environment. Keep passwords out of source control.

Run `python app.py`, or use your existing Flask hosting setup.

## Import an existing database export

Place your private `latetrack.sql` next to `app.py` and run:

```text
python app.py import-database latetrack.sql
```

Enter your MySQL password at the prompt. This command needs no `.env` file
or environment variables. For another host or user, add `--host HOST` or
`--user USER`. Use `--database NAME` to choose the destination database.

The command creates the database and missing tables, then imports missing
rows. It keeps existing rows when a primary key or username already exists.
It ignores the export's DROP statements and rolls back the imported data if
an insert fails. Newly created empty tables can remain after a failed import
because MySQL commits schema changes separately.

The export includes its stored admin password hash and migration history,
so importing it into an empty database restores that login without asking
for a new admin password. If that account already exists, the command keeps
the destination account's credentials.

Keep the SQL export private. `.gitignore` excludes it from commits. The
public repository contains the importer, not student records or login hashes.

## Admin login migration

After setting `TIMEWISE_ADMIN_PASSWORD`, restart Flask and open `/login`.
The app changes the admin username to `Dunes_TimeWise` and stores a Werkzeug
password hash for the password you supplied.

The migration prefers an existing `Dunes_TimeWise` admin, then an account
named `admin`, then a sole admin with another name. It preserves the staff ID
when updating an account. If none of these accounts match, it creates the
requested admin account and keeps the other accounts. If a non-admin owns
the requested username, the migration stops with a username conflict.

The app creates a `timewise_migrations` table and commits its marker with
the account update. The MySQL user needs permission to create that table and
update `staff_users`. Keep the migration table in database backups.

After the migration succeeds, you can remove `TIMEWISE_ADMIN_PASSWORD` from
the server environment. The database marker prevents the app from resetting
later password changes. Without this environment variable, the app leaves
existing logins unchanged.

## Registration

Search for a student name and select the matching students. Use Previous
and Next to browse results, or search for another name. Your picks remain
in the selected-students list. Remove individual picks or use Clear Form.

Record Late Arrivals saves the group with one arrival timestamp. The app
skips students already registered that day and keeps the school-day and
7:40 AM rules.

## Checks

Run `python -m unittest discover -s tests -v` with the application dependencies
installed. Tests use SQL fixtures and do not need a running MySQL server.
