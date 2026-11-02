"""Registration regressions using a local SQL fixture, without a MySQL server.

Run: python -m unittest discover -s tests -v
"""
import sqlite3
import unittest
from datetime import date, datetime
from unittest.mock import patch

from werkzeug.datastructures import MultiDict

import app as application


class FrozenDatetime(datetime):
    current = datetime(2026, 11, 2, 8, 15)

    @classmethod
    def now(cls, tz=None):
        return cls.current.replace(tzinfo=tz)


class FixtureCursor:
    def __init__(self, connection):
        self.connection = connection
        self.cursor = connection.database.cursor()

    def execute(self, sql, parameters=()):
        if "INSERT INTO late_arrivals" in sql:
            self.connection.inserts += 1
            if self.connection.fail_on_insert == self.connection.inserts:
                raise RuntimeError("Simulated insert failure")
        # These routes use SQL supported by both MySQL and SQLite.
        self.cursor.execute(sql.replace("%s", "?"), tuple(
            value.isoformat(sep=" ") if isinstance(value, datetime)
            else value.isoformat() if isinstance(value, date) else value
            for value in parameters
        ))

    def fetchone(self):
        row = self.cursor.fetchone()
        return dict(row) if row is not None else None

    def fetchall(self):
        return [dict(row) for row in self.cursor.fetchall()]

    def close(self):
        self.cursor.close()


class FixtureConnection:
    def __init__(self):
        self.database = sqlite3.connect(":memory:", check_same_thread=False)
        self.database.row_factory = sqlite3.Row
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.inserts = 0
        self.fail_on_insert = None
        self.database.executescript("""
            CREATE TABLE student_roster (
                student_no TEXT PRIMARY KEY, student_name TEXT,
                grade TEXT, section TEXT, is_active INTEGER
            );
            CREATE TABLE late_arrivals (
                arrival_id INTEGER PRIMARY KEY, student_name TEXT,
                grade TEXT, section TEXT, arrival_time TEXT, recorded_by TEXT
            );
        """)
        self.database.executemany(
            "INSERT INTO student_roster VALUES (?, ?, ?, ?, ?)",
            [(str(i), f"Alex {i:02}", "5", "A", 1) for i in range(1, 56)]
            + [("56", "Jamie Khan", "6", "B", 1),
               ("57", "Inactive Alex", "6", "B", 0)],
        )
        self.database.commit()

    def cursor(self, dictionary=False):
        return FixtureCursor(self)

    def commit(self):
        self.commits += 1
        self.database.commit()

    def rollback(self):
        self.rollbacks += 1
        self.database.rollback()

    def close(self):
        self.closed = True

    def arrivals(self):
        return [dict(row) for row in self.database.execute(
            "SELECT * FROM late_arrivals ORDER BY arrival_id"
        )]


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        application.app.config.update(TESTING=True, SECRET_KEY="test-only")
        self.connection = FixtureConnection()
        self.addCleanup(self.connection.database.close)
        self.connection_patch = patch.object(
            application, "get_connection", return_value=self.connection
        )
        self.get_connection = self.connection_patch.start()
        self.addCleanup(self.connection_patch.stop)
        FrozenDatetime.current = datetime(2026, 11, 2, 8, 15)
        clock_patch = patch.object(application, "datetime", FrozenDatetime)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)
        self.client = application.app.test_client()
        with self.client.session_transaction() as session:
            session["staff_id"] = 1
            session["username"] = "Test Staff"

    def register(self, *numbers):
        return self.client.post(
            "/", data=MultiDict(("student_no", number) for number in numbers),
            follow_redirects=True,
        )

    def test_multiple_students_share_time_and_staff(self):
        response = self.register("1", "11", "56")
        rows = self.connection.arrivals()
        self.assertEqual(len(rows), 3)
        self.assertEqual({r["arrival_time"] for r in rows}, {"2026-11-02 08:15:00"})
        self.assertEqual({r["recorded_by"] for r in rows}, {"Test Staff"})
        self.assertEqual(self.connection.commits, 1)
        self.assertTrue(self.connection.closed)
        self.assertIn(b"Recorded late arrival for 3 student(s).", response.data)

    def test_single_student_submission_still_works(self):
        response = self.register("1")
        self.assertEqual(len(self.connection.arrivals()), 1)
        self.assertIn(b"registered successfully!", response.data)

    def test_repeated_ids_are_trimmed_and_deduplicated(self):
        self.register("1", " 1 ", "", " ", "11")
        self.assertEqual(len(self.connection.arrivals()), 2)

    def test_duplicate_today_does_not_stop_other_students(self):
        self.register("1")
        response = self.register("1", "11")
        self.assertEqual(len(self.connection.arrivals()), 2)
        self.assertIn(b"Recorded late arrival for 1 student(s).", response.data)
        self.assertIn(b"Skipped 1 student(s) already registered today.", response.data)

    def test_repeat_batch_does_not_create_duplicates(self):
        self.register("1", "11")
        response = self.register("1", "11")
        self.assertEqual(len(self.connection.arrivals()), 2)
        self.assertIn(b"Recorded late arrival for 0 student(s).", response.data)
        self.assertIn(b"Skipped 2 student(s) already registered today.", response.data)

    def test_invalid_and_inactive_ids_are_skipped(self):
        response = self.register("1", "57", "999", "1') OR 1=1 --")
        self.assertEqual(len(self.connection.arrivals()), 1)
        self.assertIn(b"Skipped 3 student(s) not found in the active roster.", response.data)

    def test_missing_selection_requires_no_database_connection(self):
        response = self.register(" ", "")
        self.get_connection.assert_not_called()
        self.assertIn(b"Please select at least one student", response.data)

    def test_weekend_and_cutoff_apply_to_entire_batch(self):
        for arrival in [datetime(2026, 11, 1, 8), datetime(2026, 11, 2, 7, 39),
                        datetime(2026, 11, 2, 7, 40)]:
            with self.subTest(arrival=arrival):
                FrozenDatetime.current = arrival
                self.register("1", "11")
                self.assertEqual(self.connection.arrivals(), [])
        self.get_connection.assert_not_called()

    def test_database_error_rolls_back_whole_batch(self):
        self.connection.fail_on_insert = 2
        with self.assertRaisesRegex(RuntimeError, "Simulated"):
            self.register("1", "11")
        self.assertEqual(self.connection.arrivals(), [])
        self.assertEqual(self.connection.commits, 0)
        self.assertEqual(self.connection.rollbacks, 1)
        self.assertTrue(self.connection.closed)

    def test_paginated_search_reaches_students_past_original_limit(self):
        all_numbers = []
        for page in range(1, 7):
            result = self.client.get(f"/api/student-search?q=Alex&page={page}").json
            self.assertEqual(result["page"], page)
            self.assertEqual(result["has_more"], page < 6)
            self.assertEqual(len(result["students"]), 10 if page < 6 else 5)
            all_numbers.extend(s["student_no"] for s in result["students"])
        self.assertEqual(len(set(all_numbers)), 55)
        self.assertNotIn("57", all_numbers)

    def test_search_short_query_and_invalid_page(self):
        self.assertEqual(self.client.get("/api/student-search?q=A&page=1").json,
                         {"students": [], "page": 1, "has_more": False})
        for page in ["-2", "invalid"]:
            self.assertEqual(self.client.get(f"/api/student-search?q=Alex&page={page}").json["page"], 1)
        self.assertEqual(self.client.get("/api/student-search?q=nobody&page=1").json["students"], [])

    def test_legacy_search_array_remains_supported(self):
        result = self.client.get("/api/student-search?q=Alex").json
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 50)

    def test_routes_require_login(self):
        anonymous = application.app.test_client()
        for path, method in [("/", "get"), ("/", "post"),
                             ("/api/student-search?q=Alex&page=1", "get")]:
            response = getattr(anonymous, method)(path)
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.location.endswith("/login"))


if __name__ == "__main__":
    unittest.main()
