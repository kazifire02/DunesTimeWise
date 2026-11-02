"""One-time admin migration and login regression tests, using a SQL fixture."""
import os
import sqlite3
import unittest
from unittest.mock import patch

from werkzeug.security import check_password_hash, generate_password_hash

import app as application
import admin_migration as migration


TEST_ADMIN_PASSWORD = "test-only-migration-password"


class AdminFixtureCursor:
    def __init__(self, connection):
        self.connection = connection
        self.cursor = connection.database.cursor()

    @property
    def rowcount(self):
        return self.cursor.rowcount

    def execute(self, sql, parameters=()):
        if self.connection.fail_update and 'UPDATE staff_users' in sql:
            raise RuntimeError('Simulated account update failure')
        sql = sql.replace('ENGINE=InnoDB', '').replace('FOR UPDATE', '')
        sql = sql.replace('INSERT IGNORE', 'INSERT OR IGNORE').replace('%s', '?')
        self.cursor.execute(sql, parameters)

    def fetchall(self):
        return [dict(row) for row in self.cursor.fetchall()]

    def fetchone(self):
        row = self.cursor.fetchone()
        return dict(row) if row else None

    def close(self):
        self.cursor.close()


class AdminFixtureConnection:
    def __init__(self):
        self.database = sqlite3.connect(':memory:')
        self.database.row_factory = sqlite3.Row
        self.database.execute('''
            CREATE TABLE staff_users (
                staff_id INTEGER PRIMARY KEY,
                username TEXT COLLATE NOCASE UNIQUE NOT NULL,
                password_hash TEXT NOT NULL, role TEXT NOT NULL
            )
        ''')
        self.commits = 0
        self.rollbacks = 0
        self.fail_update = False
        self.closed = False

    def add(self, staff_id, username, role='admin', password_hash='old-hash'):
        self.database.execute('INSERT INTO staff_users VALUES (?, ?, ?, ?)',
                              (staff_id, username, password_hash, role))
        self.database.commit()

    def users(self):
        return [dict(row) for row in self.database.execute('SELECT * FROM staff_users ORDER BY staff_id')]

    def cursor(self, dictionary=False):
        return AdminFixtureCursor(self)

    def start_transaction(self):
        self.database.execute('BEGIN')

    def commit(self):
        self.commits += 1
        self.database.commit()

    def rollback(self):
        self.rollbacks += 1
        self.database.rollback()

    def close(self):
        self.closed = True


class AdminMigrationTests(unittest.TestCase):
    def setUp(self):
        env_patch = patch.dict(os.environ, {"TIMEWISE_ADMIN_PASSWORD": TEST_ADMIN_PASSWORD})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.connection = AdminFixtureConnection()
        self.addCleanup(self.connection.database.close)
        application.app.config.update(TESTING=True, SECRET_KEY='test-only')
        connection_patch = patch.object(application, 'get_connection', return_value=self.connection)
        self.get_connection = connection_patch.start()
        self.addCleanup(connection_patch.stop)
        cache_patch = patch.object(migration, '_migration_complete', False)
        cache_patch.start()
        self.addCleanup(cache_patch.stop)
        self.client = application.app.test_client()

    def run_migration(self):
        return migration.migrate_admin_login(self.get_connection)

    def test_without_password_keeps_existing_logins_and_does_not_connect(self):
        self.connection.add(1, 'admin')
        with patch.dict(os.environ):
            os.environ.pop('TIMEWISE_ADMIN_PASSWORD', None)
            self.assertFalse(self.run_migration())
            migration.ensure_admin_login(self.get_connection)
        self.get_connection.assert_not_called()
        self.assertEqual(self.connection.users()[0]['username'], 'admin')
        self.assertFalse(migration._migration_complete)

    def test_renames_legacy_admin_and_preserves_staff_id(self):
        self.connection.add(7, 'admin')
        self.connection.add(8, 'teacher', 'staff')
        self.assertTrue(self.run_migration())
        admin, teacher = self.connection.users()
        self.assertEqual(admin['staff_id'], 7)
        self.assertEqual(admin['username'], 'Dunes_TimeWise')
        self.assertTrue(check_password_hash(admin['password_hash'], TEST_ADMIN_PASSWORD))
        self.assertNotEqual(admin['password_hash'], TEST_ADMIN_PASSWORD)
        self.assertEqual(teacher['username'], 'teacher')
        self.assertEqual(teacher['password_hash'], 'old-hash')
        self.assertTrue(self.connection.closed)

    def test_updates_sole_admin_with_custom_name(self):
        self.connection.add(4, 'school_manager')
        self.run_migration()
        self.assertEqual(self.connection.users()[0]['username'], 'Dunes_TimeWise')
        self.assertEqual(self.connection.users()[0]['staff_id'], 4)

    def test_prefers_existing_requested_admin_over_legacy_admin(self):
        self.connection.add(1, 'admin')
        self.connection.add(2, 'dunes_timewise')
        self.run_migration()
        first, second = self.connection.users()
        self.assertEqual(first['password_hash'], 'old-hash')
        self.assertEqual(second['username'], 'Dunes_TimeWise')
        self.assertTrue(check_password_hash(second['password_hash'], TEST_ADMIN_PASSWORD))

    def test_creates_requested_admin_when_no_admin_exists(self):
        self.connection.add(8, 'teacher', 'staff')
        self.run_migration()
        users = self.connection.users()
        self.assertEqual(len(users), 2)
        self.assertEqual(users[1]['username'], 'Dunes_TimeWise')
        self.assertEqual(users[1]['role'], 'admin')

    def test_keeps_multiple_unrelated_admins_and_creates_requested_account(self):
        self.connection.add(1, 'school_manager')
        self.connection.add(2, 'office_manager')
        self.run_migration()
        users = self.connection.users()
        self.assertEqual([u['username'] for u in users], ['school_manager', 'office_manager', 'Dunes_TimeWise'])
        self.assertEqual([u['password_hash'] for u in users[:2]], ['old-hash', 'old-hash'])

    def test_marker_prevents_reset_after_later_password_change(self):
        self.connection.add(1, 'admin')
        self.run_migration()
        changed_hash = generate_password_hash('a-later-password')
        self.connection.database.execute('UPDATE staff_users SET password_hash = ?', (changed_hash,))
        self.connection.database.commit()
        self.assertFalse(self.run_migration())
        self.assertEqual(self.connection.users()[0]['password_hash'], changed_hash)
        count = self.connection.database.execute('SELECT COUNT(*) FROM timewise_migrations').fetchone()[0]
        self.assertEqual(count, 1)

    def test_error_rolls_back_account_and_marker_then_allows_retry(self):
        self.connection.add(1, 'admin')
        self.connection.fail_update = True
        with self.assertRaisesRegex(RuntimeError, 'Simulated'):
            self.run_migration()
        self.assertEqual(self.connection.users()[0]['username'], 'admin')
        self.assertEqual(self.connection.rollbacks, 1)
        count = self.connection.database.execute('SELECT COUNT(*) FROM timewise_migrations').fetchone()[0]
        self.assertEqual(count, 0)
        self.connection.fail_update = False
        self.assertTrue(self.run_migration())

    def test_does_not_promote_non_admin_username_collision(self):
        self.connection.add(1, 'admin')
        self.connection.add(2, 'Dunes_TimeWise', 'staff')
        with self.assertRaisesRegex(RuntimeError, 'non-admin'):
            self.run_migration()
        self.assertEqual([u['password_hash'] for u in self.connection.users()], ['old-hash', 'old-hash'])
        self.assertEqual(self.connection.users()[1]['role'], 'staff')

    def test_login_page_runs_migration_once_per_process(self):
        self.connection.add(1, 'admin')
        self.assertEqual(self.client.get('/login').status_code, 200)
        self.assertEqual(self.connection.users()[0]['username'], 'Dunes_TimeWise')
        self.client.get('/login')
        self.assertEqual(self.get_connection.call_count, 1)

    def test_new_login_sets_admin_session_and_rejects_old_credentials(self):
        self.connection.add(1, 'admin', password_hash=generate_password_hash('old-password'))
        response = self.client.post('/login', data={'username': 'admin', 'password': 'old-password'})
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Invalid username or password', response.data)
        response = self.client.post('/login', data={'username': 'Dunes_TimeWise', 'password': 'wrong'})
        self.assertEqual(response.status_code, 200)
        response = self.client.post('/login', data={'username': 'Dunes_TimeWise', 'password': TEST_ADMIN_PASSWORD})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith('/dashboard'))
        with self.client.session_transaction() as session:
            self.assertEqual(session['staff_id'], 1)
            self.assertEqual(session['username'], 'Dunes_TimeWise')
            self.assertEqual(session['role'], 'admin')


if __name__ == '__main__':
    unittest.main()
