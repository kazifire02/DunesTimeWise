"""Database export parser and import regressions using synthetic records."""
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as application
from admin_migration import read_database_export, import_database_export, _IMPORT_COLUMNS


def export_text(name="Sample Student"):
    escaped = name.replace('\\', '\\\\').replace("'", "\\'")
    return "\n".join([
        'CREATE TABLE `late_arrivals` (ignored);',
        'CREATE TABLE `staff_users` (ignored);',
        'CREATE TABLE `student_roster` (ignored);',
        'DROP TABLE IF EXISTS `student_roster`;',
        "INSERT INTO `staff_users` VALUES (1,'SampleAdmin','fixture-hash','admin','2026-11-02 08:00:00');",
        f"INSERT INTO `student_roster` VALUES ('1','{escaped}','4','A',1);",
        "INSERT INTO `timewise_migrations` VALUES ('fixture-import','2026-11-02 08:00:00');",
    ])


class ImportCursor:
    def __init__(self, con):
        self.con = con
        self.cursor = con.db.cursor()
        self.sql = []

    @property
    def rowcount(self):
        return self.cursor.rowcount

    def execute(self, sql, params=()):
        self.sql.append(sql)
        if sql.startswith('CREATE DATABASE'):
            return
        if sql.startswith('INSERT INTO'):
            self.con.insert_count += 1
            if self.con.fail_at == self.con.insert_count:
                raise RuntimeError('Simulated import failure')
        sql = re.sub(r'ENGINE=InnoDB DEFAULT CHARSET=utf8mb4', '', sql)
        sql = sql.replace('AUTO_INCREMENT', '')
        sql = re.sub(r'ON DUPLICATE KEY UPDATE .*', 'ON CONFLICT DO NOTHING', sql)
        self.cursor.execute(sql.replace('%s', '?'), params)

    def close(self):
        self.cursor.close()


class ImportConnection:
    def __init__(self):
        self.db = sqlite3.connect(':memory:')
        self.database = None
        self.insert_count = 0
        self.fail_at = None
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return ImportCursor(self)

    def start_transaction(self):
        self.db.execute('BEGIN')

    def commit(self):
        self.commits += 1
        self.db.commit()

    def rollback(self):
        self.rollbacks += 1
        self.db.rollback()

    def close(self):
        self.closed = True


class DatabaseImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.file = Path(self.temp.name) / 'sample.sql'
        self.file.write_text(export_text(), encoding='utf-8')
        self.con = ImportConnection()
        self.addCleanup(self.con.db.close)

    def test_parses_escaped_names_comments_and_semicolons_as_data(self):
        name = "Sample O'Neil; # literal -- text /* literal */ \\ end"
        self.file.write_text('/*!40101 SET SQL_MODE=0 */;\n' + export_text(name), encoding='utf-8')
        rows = read_database_export(self.file)
        self.assertEqual(rows['student_roster'][0][1], name)
        self.assertEqual(rows['late_arrivals'], [])
        self.assertEqual(rows['staff_users'][0][2], 'fixture-hash')

    def test_malformed_and_unsupported_inserts_are_rejected(self):
        for tail in ["INSERT INTO `staff_users` VALUES (1, SLEEP(5), 'x','admin',NULL);",
                     "INSERT INTO `unknown_table` VALUES (1);",
                     "INSERT INTO `student_roster` VALUES ('1');",
                     "INSERT INTO `student_roster` VALUES ('unfinished",
                     "INSERT INTO `student_roster` VALUES ('1','a','4','A',1),;"]:
            with self.subTest(tail=tail):
                self.file.write_text(export_text() + '\n' + tail, encoding='utf-8')
                with self.assertRaises(ValueError):
                    read_database_export(self.file)

    def test_incomplete_dump_is_rejected(self):
        self.file.write_text('SELECT 1;', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'complete'):
            read_database_export(self.file)

    def test_import_preserves_credentials_marker_and_existing_data_on_rerun(self):
        rows = read_database_export(self.file)
        counts = import_database_export(self.con, 'latetrack', rows)
        self.assertEqual(counts['student_roster'], {'inserted': 1, 'skipped': 0})
        self.assertEqual(self.con.database, 'latetrack')
        self.con.db.execute("UPDATE staff_users SET password_hash = 'changed-later'")
        self.con.db.execute("UPDATE student_roster SET student_name = 'Updated Name'")
        self.con.db.commit()
        counts = import_database_export(self.con, 'latetrack', rows)
        self.assertEqual(counts['student_roster'], {'inserted': 0, 'skipped': 1})
        self.assertEqual(self.con.db.execute('SELECT password_hash FROM staff_users').fetchone()[0], 'changed-later')
        self.assertEqual(self.con.db.execute('SELECT student_name FROM student_roster').fetchone()[0], 'Updated Name')
        self.assertEqual(self.con.db.execute('SELECT COUNT(*) FROM timewise_migrations').fetchone()[0], 1)

    def test_literal_sql_in_a_name_cannot_drop_a_table(self):
        self.file.write_text(export_text("'; DROP TABLE staff_users; --"), encoding='utf-8')
        rows = read_database_export(self.file)
        import_database_export(self.con, 'latetrack', rows)
        self.assertEqual(self.con.db.execute('SELECT COUNT(*) FROM staff_users').fetchone()[0], 1)
        self.assertEqual(self.con.db.execute('SELECT student_name FROM student_roster').fetchone()[0], "'; DROP TABLE staff_users; --")

    def test_insert_failure_rolls_back_data_in_all_tables(self):
        rows = read_database_export(self.file)
        self.con.fail_at = 2
        with self.assertRaisesRegex(RuntimeError, 'Simulated'):
            import_database_export(self.con, 'latetrack', rows)
        for table in _IMPORT_COLUMNS:
            self.assertEqual(self.con.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)
        self.assertEqual(self.con.rollbacks, 1)
        self.assertEqual(self.con.commits, 0)

    def test_database_name_is_validated(self):
        with self.assertRaises(ValueError):
            import_database_export(self.con, 'latetrack`; DROP DATABASE x', {})

    def test_cli_prompts_without_environment_variables_and_closes_connection(self):
        application.app.config['TESTING'] = True
        with patch.dict('os.environ', {}, clear=True), patch.object(application.mysql.connector, 'connect', return_value=self.con) as connect:
            result = application.app.test_cli_runner().invoke(
                args=['import-database', str(self.file)], input='fixture-password\n'
            )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn('student_roster: 1 imported', result.output)
        self.assertNotIn('fixture-password', result.output)
        self.assertEqual(connect.call_args.kwargs['password'], 'fixture-password')
        self.assertTrue(self.con.closed)

    def test_cli_rejects_invalid_export_before_connecting(self):
        self.file.write_text('SELECT 1;', encoding='utf-8')
        with patch.object(application.mysql.connector, 'connect') as connect:
            result = application.app.test_cli_runner().invoke(
                args=['import-database', str(self.file)], input='fixture-password\n'
            )
        self.assertEqual(result.exit_code, 1)
        connect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
