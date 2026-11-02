
from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, session, jsonify, send_file
)
import mysql.connector
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from io import BytesIO
from calendar import monthrange
from collections import Counter
from functools import wraps
from werkzeug.security import check_password_hash
from admin_migration import ensure_admin_login
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.chart import BarChart, Reference
from openpyxl.utils import get_column_letter
import os


# =====================================================
# FLASK CONFIGURATION
# =====================================================

app = Flask(__name__)

app.secret_key = os.environ.get("LATETRACK_SECRET_KEY")

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8)
)



# =====================================================
# DATABASE CONNECTION
# =====================================================

def get_connection():
    return mysql.connector.connect(
        host="localhost",
        user="root",
        password=os.environ.get("LATETRACK_DB_PASSWORD", ""),
        database="latetrack"
    )


# =====================================================
# LOGIN PROTECTION
# =====================================================

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "staff_id" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


# =====================================================
# STAFF LOGIN
# =====================================================

@app.route("/login", methods=["GET", "POST"])
def login():
    ensure_admin_login(get_connection)

    if request.method == "POST":
        username = request.form.get(
            "username", ""
        ).strip()

        password = request.form.get(
            "password", ""
        )

        con = get_connection()
        cursor = con.cursor(dictionary=True)

        try:
            cursor.execute(
                """
                SELECT *
                FROM staff_users
                WHERE username = %s
                """,
                (username,)
            )
            staff = cursor.fetchone()
        finally:
            cursor.close()
            con.close()

        if staff and check_password_hash(
            staff["password_hash"], password
        ):
            session.clear()
            session.permanent = True
            session["staff_id"] = staff["staff_id"]
            session["username"] = staff["username"]
            session["role"] = staff["role"]

            return redirect(url_for("dashboard"))

        flash("Invalid username or password.")

    return render_template("login.html")


# =====================================================
# LOGOUT
# =====================================================

@app.route("/logout", methods=["POST"])
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


# =====================================================
# STUDENT NAME SEARCH - ALL GRADES
# =====================================================

@app.route("/api/student-search")
@login_required
def student_search_api():

    query = request.args.get("q", "").strip()
    paginated = "page" in request.args
    page = max(1, request.args.get("page", 1, type=int))
    page_size = 10

    if len(query) < 2:
        if paginated:
            return jsonify(students=[], page=page, has_more=False)
        return jsonify([])

    con = get_connection()
    cursor = con.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            SELECT
                student_no,
                student_name,
                grade,
                section
            FROM student_roster
            WHERE is_active = 1
            AND student_name LIKE %s
            ORDER BY student_name, grade, section, student_no
            LIMIT %s OFFSET %s
            """,
            (
                "%" + query + "%",
                page_size + 1 if paginated else 50,
                (page - 1) * page_size if paginated else 0
            )
        )

        students = cursor.fetchall()
        has_more = paginated and len(students) > page_size
        if paginated:
            students = students[:page_size]

        for student in students:
            student["student_no"] = str(
                student["student_no"]
            )

        if paginated:
            return jsonify(students=students, page=page, has_more=has_more)
        return jsonify(students)

    finally:
        cursor.close()
        con.close()




# =====================================================
# STUDENT LATE ARRIVAL REGISTRATION
# =====================================================

@app.route("/", methods=["GET", "POST"])
@login_required
def register():

    if request.method == "POST":

        student_numbers = list(dict.fromkeys(
            number.strip()
            for number in request.form.getlist("student_no")
            if number.strip()
        ))

        if not student_numbers:
            flash("Please select at least one student from the list.")
            return redirect(url_for("register"))

        arrival_time = datetime.now(
            ZoneInfo("Asia/Dubai")
        ).replace(tzinfo=None)

        is_school_day = arrival_time.weekday() < 5

        cutoff_time = arrival_time.replace(
            hour=7,
            minute=40,
            second=0,
            microsecond=0
        )

        # Only Monday to Friday
        if not is_school_day:
            flash(
                "Late arrival registration is available "
                "only Monday to Friday."
            )
            return redirect(url_for("register"))

        # Only after 7:40 AM
        if arrival_time <= cutoff_time:
            flash(
                "Late arrivals are recorded only after 7:40 AM."
            )
            return redirect(url_for("register"))

        con = get_connection()
        cursor = con.cursor(dictionary=True)
        registered = []
        already_registered = 0

        try:
            # Resolve all selected IDs against the active roster.
            placeholders = ", ".join(["%s"] * len(student_numbers))
            cursor.execute(
                f"""
                SELECT
                    student_no,
                    student_name,
                    grade,
                    section
                FROM student_roster
                WHERE student_no IN ({placeholders})
                  AND is_active = 1
                """,
                tuple(student_numbers)
            )

            students = cursor.fetchall()
            invalid_count = len(student_numbers) - len(students)

            for student in students:
                student_name = student["student_name"]
                grade = student["grade"]
                section = student["section"]

                # Skip today's duplicates without stopping the batch.
                cursor.execute(
                    """
                    SELECT arrival_id
                    FROM late_arrivals
                    WHERE LOWER(TRIM(student_name)) = LOWER(%s)
                      AND grade = %s
                      AND section = %s
                      AND DATE(arrival_time) = %s
                    LIMIT 1
                    """,
                    (student_name, grade, section, arrival_time.date())
                )

                if cursor.fetchone():
                    already_registered += 1
                    continue

                cursor.execute(
                    """
                    INSERT INTO late_arrivals
                    (
                        student_name,
                        grade,
                        section,
                        arrival_time,
                        recorded_by
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        student_name, grade, section,
                        arrival_time, session["username"]
                    )
                )
                registered.append(student)

            con.commit()

        except Exception:
            con.rollback()
            raise

        finally:
            cursor.close()
            con.close()

        if len(student_numbers) == 1 and registered:
            student = registered[0]
            flash(
                f"{student['student_name']} "
                f"(Grade {student['grade']}-{student['section']}) "
                "registered successfully!"
            )
        else:
            messages = [
                f"Recorded late arrival for {len(registered)} student(s)."
            ]
            if already_registered:
                messages.append(
                    f"Skipped {already_registered} student(s) "
                    "already registered today."
                )
            if invalid_count:
                messages.append(
                    f"Skipped {invalid_count} student(s) "
                    "not found in the active roster."
                )
            flash(" ".join(messages))

        return redirect(url_for("register"))

    return render_template("register.html")


# =====================================================
# NEW STUDENT ADMISSION
# =====================================================

@app.route("/new-admission", methods=["GET", "POST"])
@login_required
def new_admission():

    # Restrict roster changes to administrators.
    if session.get("role", "").lower() != "admin":
        return "Access denied. Admin only.", 403

    if request.method == "POST":

        student_no = request.form.get(
            "student_no", ""
        ).strip()

        student_name = " ".join(
            request.form.get(
                "student_name", ""
            ).strip().split()
        )

        grade = request.form.get(
            "grade", ""
        ).strip()

        section = request.form.get(
            "section", ""
        ).strip().upper()

        valid_grades = (
            ["KG1", "KG2"] +
            [str(i) for i in range(1, 13)]
        )

        valid_sections = [
            "A", "B", "C", "D", "E", "F"
        ]

        if not student_no or not student_name:
            flash("Please complete all student details.")
            return redirect(url_for("new_admission"))

        if (
            grade not in valid_grades
            or section not in valid_sections
        ):
            flash("Please select a valid grade and section.")
            return redirect(url_for("new_admission"))

        con = get_connection()
        cursor = con.cursor(dictionary=True)

        try:

            # Check for an existing student number
            cursor.execute(
                """
                SELECT student_no
                FROM student_roster
                WHERE student_no = %s
                LIMIT 1
                """,
                (student_no,)
            )

            existing = cursor.fetchone()

            if existing:
                flash(
                    "This student number already exists. "
                    "Please check the admission details."
                )
                return redirect(url_for("new_admission"))

            # Insert student into school roster
            cursor.execute(
                """
                INSERT INTO student_roster
                (
                    student_no,
                    student_name,
                    grade,
                    section
                )
                VALUES (%s, %s, %s, %s)
                """,
                (
                    student_no,
                    student_name,
                    grade,
                    section
                )
            )

            con.commit()

        finally:
            cursor.close()
            con.close()

        flash(
            f"{student_name} (Grade {grade}-{section}) "
            "has been added successfully!"
        )

        return redirect(url_for("new_admission"))

    return render_template("new_admission.html")


@app.route("/remove-student", methods=["GET", "POST"])
@login_required
def remove_student():
    # Only administrators can remove students
    if session.get("role", "").lower() != "admin":
        return "Access denied. Admin only.", 403

    student = None
    search_number = ""

    if request.method == "POST":
        student_no = request.form.get("student_no", "").strip()

        if not student_no:
            flash("Admission number is required.")
            return redirect(url_for("remove_student"))

        con = get_connection()
        cursor = con.cursor(dictionary=True)

        try:
            # Check that the student is currently active
            cursor.execute(
                """
                SELECT student_no, student_name, grade, section
                FROM student_roster
                WHERE student_no = %s AND is_active = 1
                LIMIT 1
                """,
                (student_no,)
            )

            student = cursor.fetchone()

            if not student:
                flash("Student not found or already removed.")
                return redirect(url_for("remove_student"))

            # Mark student inactive, preserving history
            cursor.execute(
                """
                UPDATE student_roster
                SET is_active = 0
                WHERE student_no = %s AND is_active = 1
                """,
                (student_no,)
            )

            con.commit()

            flash(
                f"{student['student_name']} "
                f"(Admission No: {student_no}) "
                "has been removed from the active roster."
            )

            return redirect(url_for("remove_student"))

        finally:
            cursor.close()
            con.close()

    # GET: Find a student using admission number
    search_number = request.args.get("student_no", "").strip()

    if search_number:
        con = get_connection()
        cursor = con.cursor(dictionary=True)

        try:
            cursor.execute(
                """
                SELECT student_no, student_name, grade, section
                FROM student_roster
                WHERE student_no = %s AND is_active = 1
                LIMIT 1
                """,
                (search_number,)
            )

            student = cursor.fetchone()

        finally:
            cursor.close()
            con.close()

    return render_template(
        "remove_student.html",
        student=student,
        search_number=search_number
    )



# =====================================================
# PUNCTUALITY ALERT CALCULATIONS
# =====================================================

from collections import defaultdict


def next_school_day(day):
    """Next Monday-Friday date."""
    day += timedelta(days=1)

    while day.weekday() >= 5:
        day += timedelta(days=1)

    return day


def get_punctuality_alerts(year=None, month=None):
    today = datetime.now(
        ZoneInfo("Asia/Dubai")
    ).date()

    year = year or today.year
    month = month or today.month

    start = date(year, month, 1)

    if month == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month + 1, 1)

    con = get_connection()
    cursor = con.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            SELECT
                student_name,
                grade,
                section,
                arrival_time
            FROM late_arrivals
            WHERE arrival_time >= %s
              AND arrival_time < %s
            ORDER BY arrival_time ASC
            """,
            (start, end)
        )

        records = cursor.fetchall()

    finally:
        cursor.close()
        con.close()

    grouped = defaultdict(list)

    for record in records:
        key = (
            record["student_name"].strip().casefold(),
            str(record["grade"]),
            str(record["section"])
        )

        grouped[key].append(record)

    alerts = []

    for key, student_records in grouped.items():

        student_records.sort(
            key=lambda r: r["arrival_time"]
        )

        # One record per distinct late-arrival date
        daily_records = {}

        for record in student_records:
            day = record["arrival_time"].date()
            daily_records[day] = record

        days = sorted(daily_records)

        # Find all consecutive school-day runs
        runs = []
        current_run = []

        for day in days:

            if (
                current_run
                and next_school_day(
                    current_run[-1]
                ) != day
            ):
                runs.append(current_run)
                current_run = []

            current_run.append(day)

        if current_run:
            runs.append(current_run)

        qualifying_runs = [
            run for run in runs
            if len(run) >= 3
        ]

        # Display the longest qualifying run.
        # If tied, prefer the latest run.
        longest_run = max(
            qualifying_runs,
            key=lambda run: (
                len(run),
                run[-1]
            ),
            default=[]
        )

        monthly_count = len(days)

        is_consecutive = bool(qualifying_runs)
        is_monthly = monthly_count >= 2

        if not is_consecutive and not is_monthly:
            continue

        if is_consecutive and is_monthly:
            alert_type = "both"
        elif is_consecutive:
            alert_type = "consecutive"
        else:
            alert_type = "monthly"

        arrival_details = [
            {
                "date": day.strftime("%d %B %Y"),
                "day": day.strftime("%A"),
                "time": daily_records[day][
                    "arrival_time"
                ].strftime("%I:%M %p"),
                "is_consecutive": day in longest_run
            }
            for day in days
        ]

        alerts.append({
            "name": student_records[0]["student_name"],
            "grade": student_records[0]["grade"],
            "section": student_records[0]["section"],
            "type": alert_type,
            "monthly_count": monthly_count,
            "consecutive_count": len(longest_run),
            "consecutive_dates": [
                day.strftime("%d %B %Y")
                for day in longest_run
            ],
            "arrival_details": arrival_details,
            "last_arrival": max(days)
        })

    alerts.sort(
        key=lambda a: a["last_arrival"],
        reverse=True
    )

    return {
        "alerts": alerts,
        "total": len(alerts),
        "consecutive_total": sum(
            a["consecutive_count"] >= 3
            for a in alerts
        ),
        "monthly_total": sum(
            a["monthly_count"] >= 2
            for a in alerts
        )
    }


# =====================================================
# PUNCTUALITY NOTIFICATIONS PAGE
# =====================================================

@app.route("/notifications")
@login_required
def notifications():

    today = datetime.now(
        ZoneInfo("Asia/Dubai")
    ).date()

    try:
        year = int(
            request.args.get("year", today.year)
        )

        month = int(
            request.args.get("month", today.month)
        )

        if not 2000 <= year <= 2100:
            raise ValueError

        if not 1 <= month <= 12:
            raise ValueError

    except ValueError:
        return "Invalid month or year.", 400

    data = get_punctuality_alerts(year, month)

    return render_template(
        "notifications.html",
        alerts=data["alerts"],
        alert_total=data["total"],
        consecutive_total=data["consecutive_total"],
        monthly_total=data["monthly_total"],
        selected_year=year,
        selected_month=month,
        month_label=date(
            year, month, 1
        ).strftime("%B %Y")
    )


# =====================================================
# DASHBOARD
# =====================================================

@app.route("/dashboard")
@login_required
def dashboard():

    now = datetime.now(ZoneInfo("Asia/Dubai"))

    today = now.date()
    yesterday = today - timedelta(days=1)

    month_start = today.replace(day=1)

    if today.month == 12:
        next_month = today.replace(
            year=today.year + 1,
            month=1,
            day=1
        )
    else:
        next_month = today.replace(
            month=today.month + 1,
            day=1
        )

    con = get_connection()
    cursor = con.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            SELECT
                arrival_id,
                student_name,
                grade,
                section,
                arrival_time
            FROM late_arrivals
            WHERE arrival_time >= %s
              AND arrival_time < %s
            ORDER BY arrival_time DESC, arrival_id DESC
            """,
            (today, today + timedelta(days=1))
        )

        students = cursor.fetchall()

        cursor.execute(
            """
            SELECT COUNT(*) AS total
            FROM late_arrivals
            WHERE arrival_time >= %s
              AND arrival_time < %s
            """,
            (yesterday, today)
        )

        yesterday_total = cursor.fetchone()["total"]

        cursor.execute(
            """
            SELECT COUNT(*) AS total
            FROM late_arrivals
            WHERE arrival_time >= %s
              AND arrival_time < %s
            """,
            (month_start, next_month)
        )

        month_total = cursor.fetchone()["total"]

    finally:
        cursor.close()
        con.close()
    alert_data = get_punctuality_alerts(
        today.year,
        today.month
    )

    return render_template(
        "dashboard.html",
        students=students,
        total=len(students),
        yesterday_total=yesterday_total,
        month_total=month_total,
        today=today.strftime("%d %B %Y"),
        alert_total=alert_data["total"],
        consecutive_total=alert_data["consecutive_total"],
        monthly_total=alert_data["monthly_total"]
    )



# =====================================================
# DAILY REPORT DATA
# =====================================================

def get_report_data(report_date, grade="", section=""):

    start_date = datetime.strptime(
        report_date,
        "%Y-%m-%d"
    )

    end_date = start_date + timedelta(days=1)

    query = """
        SELECT
            student_name,
            grade,
            section,
            arrival_time
        FROM late_arrivals
        WHERE arrival_time >= %s
          AND arrival_time < %s
    """

    params = [start_date, end_date]

    if grade:
        query += " AND grade = %s"
        params.append(grade)

    if section:
        query += " AND section = %s"
        params.append(section)

    query += " ORDER BY arrival_time ASC"

    con = get_connection()
    cursor = con.cursor(dictionary=True)

    try:
        cursor.execute(query, tuple(params))
        return cursor.fetchall()
    finally:
        cursor.close()
        con.close()


# =====================================================
# DAILY REPORT PAGE
# =====================================================

@app.route("/reports")
@login_required
def reports():

    today = datetime.now(
        ZoneInfo("Asia/Dubai")
    ).strftime("%Y-%m-%d")

    report_date = request.args.get("date", today)
    grade = request.args.get("grade", "")
    section = request.args.get("section", "")

    try:
        datetime.strptime(report_date, "%Y-%m-%d")
    except ValueError:
        return "Invalid report date.", 400

    valid_grades = (
        ["KG1", "KG2"] +
        [str(i) for i in range(1, 13)]
    )

    if grade and grade not in valid_grades:
        return "Invalid grade.", 400

    if section and section not in "ABCDEF":
        return "Invalid section.", 400

    students = get_report_data(
        report_date,
        grade,
        section
    )

    return render_template(
        "reports.html",
        students=students,
        total=len(students),
        report_date=report_date,
        selected_grade=grade,
        selected_section=section
    )


# =====================================================
# DAILY EXCEL EXPORT
# =====================================================

@app.route("/export")
@login_required
def export_report():

    today = datetime.now(
        ZoneInfo("Asia/Dubai")
    ).strftime("%Y-%m-%d")

    report_date = request.args.get("date", today)
    grade = request.args.get("grade", "")
    section = request.args.get("section", "")

    try:
        datetime.strptime(report_date, "%Y-%m-%d")
    except ValueError:
        return "Invalid report date.", 400

    valid_grades = (
        ["KG1", "KG2"] +
        [str(i) for i in range(1, 13)]
    )

    if grade and grade not in valid_grades:
        return "Invalid grade.", 400

    if section and section not in "ABCDEF":
        return "Invalid section.", 400

    students = get_report_data(
        report_date,
        grade,
        section
    )

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Late Arrivals"

    sheet.append(["DUNES TIMEWISE"])
    sheet.append(["Every Minute Matters."])
    sheet.append(["Date", report_date])
    sheet.append(["Total Late Arrivals", len(students)])
    sheet.append([])

    headers = [
        "No.",
        "Student Name",
        "Grade",
        "Section",
        "Arrival Time"
    ]

    sheet.append(headers)

    for index, student in enumerate(students, 1):
        sheet.append([
            index,
            student["student_name"],
            student["grade"],
            student["section"],
            student["arrival_time"].strftime(
                "%I:%M:%S %p"
            )
        ])

    for cell in sheet[6]:
        cell.font = Font(
            color="FFFFFF",
            bold=True
        )
        cell.fill = PatternFill(
            "solid",
            fgColor="800020"
        )
        cell.alignment = Alignment(
            horizontal="center"
        )

    for column, width in {
        "A": 10,
        "B": 35,
        "C": 15,
        "D": 15,
        "E": 22
    }.items():
        sheet.column_dimensions[column].width = width

    sheet.freeze_panes = "A7"
    sheet.auto_filter.ref = (
        f"A6:E{max(6, sheet.max_row)}"
    )

    output = BytesIO()
    workbook.save(output)
    output.seek(0)

    return send_file(
        output,
        as_attachment=True,
        download_name=(
            f"TimeWise_Daily_{report_date}.xlsx"
        ),
        mimetype=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        )
    )


# =====================================================
# STUDENT ROSTER API
# =====================================================

@app.route("/api/roster")
@login_required
def roster_api():

    grade = request.args.get(
        "grade", ""
    ).strip()

    section = request.args.get(
        "section", ""
    ).strip().upper()

    if not grade:
        return jsonify([])

    con = get_connection()
    cursor = con.cursor(dictionary=True)

    try:
        if not section:
            cursor.execute(
                """
                SELECT DISTINCT section
                FROM student_roster
                WHERE grade = %s
                ORDER BY section
                """,
                (grade,)
            )
        else:
            cursor.execute(
                """
                SELECT student_no, student_name
                FROM student_roster
                WHERE grade = %s
                  AND section = %s
                ORDER BY student_name
                """,
                (grade, section)
            )

        return jsonify(cursor.fetchall())

    finally:
        cursor.close()
        con.close()


# =====================================================
# STUDENT HISTORY
# =====================================================


@app.route("/student-history")
@login_required
def student_history():

    grade = request.args.get("grade", "").strip()
    section = request.args.get("section", "").strip().upper()
    student_no = request.args.get("student_no", "").strip()

    students = []
    total = 0
    error = ""

    # Display all grades when no filters are selected
    searched = True
    selected_name = ""

    if student_no and (not grade or not section):
        error = (
            "Please select a grade and section "
            "before selecting a student."
        )

    if not error:
        con = get_connection()
        cursor = con.cursor(dictionary=True)

        try:
            if student_no:
                cursor.execute("""
                    SELECT student_name
                    FROM student_roster
                    WHERE student_no = %s
                      AND grade = %s
                      AND section = %s
                    LIMIT 1
                """, (student_no, grade, section))

                student = cursor.fetchone()

                if not student:
                    error = "Selected student was not found."
                else:
                    selected_name = student["student_name"]

            if not error:
                query = """
                    SELECT
                        student_name,
                        grade,
                        section,
                        arrival_time,
                        recorded_by
                    FROM late_arrivals
                    WHERE 1 = 1
                """

                params = []

                if grade:
                    query += " AND grade = %s"
                    params.append(grade)

                if section:
                    query += " AND section = %s"
                    params.append(section)

                if selected_name:
                    query += """
                        AND LOWER(TRIM(student_name)) = LOWER(%s)
                    """
                    params.append(selected_name)

                query += """
                    ORDER BY arrival_time DESC
                """

                cursor.execute(query, tuple(params))
                students = cursor.fetchall()
                total = len(students)

        finally:
            cursor.close()
            con.close()

    return render_template(
        "student_history.html",
        students=students,
        total=total,
        grade=grade,
        section=section,
        student_no=student_no,
        name=selected_name,
        searched=searched,
        error=error
    )


# =====================================================
# ANALYTICS PERIOD SELECTION
# =====================================================

def get_analytics_period():

    today = datetime.now(
        ZoneInfo("Asia/Dubai")
    ).date()

    period = request.args.get(
        "period", "weekly"
    ).lower()

    if period not in (
        "weekly", "monthly", "yearly"
    ):
        period = "weekly"

    try:
        year = int(
            request.args.get("year", today.year)
        )

        month = int(
            request.args.get("month", today.month)
        )

        week = int(
            request.args.get(
                "week",
                today.isocalendar().week
            )
        )

        if not 2000 <= year <= 2100:
            raise ValueError()

        if not 1 <= month <= 12:
            raise ValueError()

        if not 1 <= week <= 53:
            raise ValueError()

        if period == "weekly":

            start = date.fromisocalendar(
                year, week, 1
            )

            end = start + timedelta(days=7)

            label = (
                f"Week {week}, {year} "
                f"({start:%d %b} - "
                f"{end - timedelta(days=1):%d %b %Y})"
            )

        elif period == "monthly":

            start = date(year, month, 1)

            if month == 12:
                end = date(year + 1, 1, 1)
            else:
                end = date(year, month + 1, 1)

            label = start.strftime("%B %Y")

        else:

            start = date(year, 1, 1)
            end = date(year + 1, 1, 1)

            label = f"Year {year}"

    except (ValueError, OverflowError):
        raise ValueError("Invalid analytics period.")

    return {
        "period": period,
        "year": year,
        "month": month,
        "week": week,
        "start": start,
        "end": end,
        "label": label
    }


# =====================================================
# FETCH ANALYTICS DATA
# =====================================================

def get_analytics_data(start, end):

    con = get_connection()
    cursor = con.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            SELECT
                student_name,
                grade,
                section,
                arrival_time,
                recorded_by
            FROM late_arrivals
            WHERE arrival_time >= %s
              AND arrival_time < %s
            ORDER BY arrival_time ASC
            """,
            (start, end)
        )

        return cursor.fetchall()

    finally:
        cursor.close()
        con.close()


# =====================================================
# ANALYTICS CALCULATIONS
# =====================================================

def calculate_analytics(records, settings):

    period = settings["period"]
    start = settings["start"]

    student_counts = Counter(
        (
            row["student_name"].strip().lower(),
            str(row["grade"]),
            str(row["section"])
        )
        for row in records
    )

    unique_students = len(student_counts)

    repeat_students = sum(
        1
        for count in student_counts.values()
        if count >= 3
    )

    grade_counts = Counter(
        str(row["grade"])
        for row in records
    )

    def grade_key(item):
        grade = item[0]

        if grade == "KG1":
            return -1

        if grade == "KG2":
            return 0

        if grade.isdigit():
            return int(grade)

        return 99

    grade_stats = [
        {
            "grade": grade,
            "count": count
        }
        for grade, count in sorted(
            grade_counts.items(),
            key=grade_key
        )
    ]

    student_details = {}

    for row in records:

        key = (
            row["student_name"].strip().lower(),
            str(row["grade"]),
            str(row["section"])
        )

        if key not in student_details:
            student_details[key] = {
                "name": row["student_name"],
                "grade": row["grade"],
                "section": row["section"],
                "count": 0
            }

        student_details[key]["count"] += 1

    student_stats = sorted(
        student_details.values(),
        key=lambda item: (
            -item["count"],
            item["name"].lower()
        )
    )

    trend = []

    if period == "weekly":

        for offset in range(7):

            current_day = start + timedelta(
                days=offset
            )

            count = sum(
                1
                for row in records
                if row["arrival_time"].date() ==
                   current_day
            )

            trend.append({
                "label": current_day.strftime("%a"),
                "count": count
            })

    elif period == "monthly":

        days = monthrange(
            settings["year"],
            settings["month"]
        )[1]

        for day in range(1, days + 1):

            current_day = date(
                settings["year"],
                settings["month"],
                day
            )

            count = sum(
                1
                for row in records
                if row["arrival_time"].date() ==
                   current_day
            )

            trend.append({
                "label": str(day),
                "count": count
            })

    else:

        for month_number in range(1, 13):

            count = sum(
                1
                for row in records
                if row["arrival_time"].month ==
                   month_number
            )

            month_name = date(
                settings["year"],
                month_number,
                1
            ).strftime("%b")

            trend.append({
                "label": month_name,
                "count": count
            })

    return {
        "total_arrivals": len(records),
        "unique_students": unique_students,
        "repeat_students": repeat_students,
        "grade_stats": grade_stats,
        "student_stats": student_stats,
        "trend": trend
    }


# =====================================================
# ANALYTICS PAGE
# =====================================================

@app.route("/analytics")
@login_required
def analytics():

    try:
        settings = get_analytics_period()
    except ValueError:
        return "Invalid analytics period.", 400

    records = get_analytics_data(
        settings["start"],
        settings["end"]
    )

    stats = calculate_analytics(
        records,
        settings
    )

    return render_template(
        "analytics.html",
        settings=settings,
        stats=stats,
        current_year=datetime.now(
            ZoneInfo("Asia/Dubai")
        ).year,
        timedelta=timedelta
    )


# =====================================================
# EXCEL REPORTING PERIODS
# =====================================================

def excel_period_buckets(settings):

    start = settings["start"]
    end = settings["end"]
    period = settings["period"]

    buckets = []

    if period == "weekly":

        for i in range(7):

            day = start + timedelta(days=i)

            buckets.append((
                day.strftime("%A"),
                day,
                day + timedelta(days=1),
                day.strftime("%d %b %Y")
            ))

    elif period == "monthly":

        day = start
        number = 1

        while day < end:

            next_monday = day + timedelta(
                days=7 - day.weekday()
            )

            finish = min(next_monday, end)

            last_day = finish - timedelta(days=1)

            label = (
                f"{day:%d %b} - "
                f"{last_day:%d %b}"
            )

            buckets.append((
                f"Week {number}",
                day,
                finish,
                label
            ))

            day = finish
            number += 1

    else:

        for month in range(1, 13):

            day = date(
                settings["year"],
                month,
                1
            )

            if month == 12:
                finish = date(
                    settings["year"] + 1,
                    1,
                    1
                )
            else:
                finish = date(
                    settings["year"],
                    month + 1,
                    1
                )

            buckets.append((
                day.strftime("%B"),
                day,
                finish,
                str(settings["year"])
            ))

    return buckets


# =====================================================
# EXCEL STUDENT IDENTIFICATION
# =====================================================

def excel_student_key(record):

    return (
        record["student_name"].strip().casefold(),
        str(record["grade"]).strip().upper(),
        str(record["section"]).strip().upper()
    )


# =====================================================
# EXCEL GRADE SORTING
# =====================================================

def excel_grade_key(grade):

    grade = str(grade).upper()

    if grade == "KG1":
        return (0, 1)

    if grade == "KG2":
        return (0, 2)

    if grade.isdigit():
        return (1, int(grade))

    return (2, grade)


# =====================================================
# EXCEL SCHOOL HEADER
# =====================================================

def excel_sheet_title(
    sheet,
    heading,
    period_label
):

    sheet.merge_cells("A1:F1")
    sheet["A1"] = "DUNES TIMEWISE"

    sheet["A1"].font = Font(
        name="Calibri",
        size=20,
        bold=True,
        color="800020"
    )

    sheet.merge_cells("A2:F2")
    sheet["A2"] = "Every Minute Matters."

    sheet["A2"].font = Font(
        italic=True,
        color="666666"
    )

    sheet.merge_cells("A3:F3")
    sheet["A3"] = heading

    sheet["A3"].font = Font(
        size=14,
        bold=True,
        color="800020"
    )

    sheet.merge_cells("A4:F4")

    sheet["A4"] = (
        f"Reporting Period: {period_label}"
    )

    sheet["A4"].font = Font(
        bold=True,
        color="444444"
    )

    sheet.row_dimensions[1].height = 34
    sheet.sheet_view.showGridLines = False


# =====================================================
# EXCEL TABLE FORMATTING
# =====================================================

def excel_format_table(
    sheet,
    header_row,
    columns,
    total_row=None
):

    for cell in sheet[header_row][:columns]:

        cell.fill = PatternFill(
            "solid",
            fgColor="800020"
        )

        cell.font = Font(
            color="FFFFFF",
            bold=True
        )

        cell.alignment = Alignment(
            horizontal="center",
            vertical="center"
        )

    sheet.row_dimensions[header_row].height = 27

    for row in sheet.iter_rows(
        min_row=header_row + 1,
        max_row=sheet.max_row,
        max_col=columns
    ):

        if row[0].row % 2 == 0:

            for cell in row:
                cell.fill = PatternFill(
                    "solid",
                    fgColor="FFF8F0"
                )

    if total_row is not None:

        for cell in sheet[total_row][:columns]:

            cell.fill = PatternFill(
                "solid",
                fgColor="F4DFE5"
            )

            cell.font = Font(
                bold=True,
                color="800020"
            )

    for col in range(1, columns + 1):

        letter = get_column_letter(col)

        width = max(
            (
                len(
                    str(
                        sheet.cell(
                            row,
                            col
                        ).value or ""
                    )
                )
                for row in range(
                    1,
                    sheet.max_row + 1
                )
            ),
            default=12
        )

        sheet.column_dimensions[letter].width = min(
            max(width + 3, 15),
            38
        )

    sheet.freeze_panes = (
        f"B{header_row + 1}"
    )

    last = (
        total_row - 1
        if total_row is not None
        else sheet.max_row
    )

    if last >= header_row:

        sheet.auto_filter.ref = (
            f"A{header_row}:"
            f"{get_column_letter(columns)}{last}"
        )


# =====================================================
# WEEKLY / MONTHLY / YEARLY EXCEL EXPORT
# =====================================================

@app.route("/analytics/export")
@login_required
def export_analytics():

    try:
        settings = get_analytics_period()
    except ValueError:
        return "Invalid analytics period.", 400

    records = get_analytics_data(
        settings["start"],
        settings["end"]
    )

    buckets = excel_period_buckets(settings)

    report_type = settings["period"]

    unique_students = len({
        excel_student_key(record)
        for record in records
    })

    workbook = Workbook()

    # =================================================
    # SHEET 1: OVERVIEW
    # =================================================

    overview = workbook.active
    overview.title = "Overview"

    excel_sheet_title(
        overview,
        f"{report_type.title()} Punctuality Report",
        settings["label"]
    )

    summary_rows = [
        (6, "Total Late Arrivals", len(records)),
        (7, "Unique Students Late", unique_students),
        (8, "Report Type", report_type.title()),
        (9, "Start Date", settings["start"]),
        (
            10,
            "End Date",
            settings["end"] - timedelta(days=1)
        )
    ]

    for row_number, label, value in summary_rows:

        overview.cell(
            row_number,
            1,
            label
        ).font = Font(
            bold=True,
            color="800020"
        )

        overview.cell(
            row_number,
            2,
            value
        )

    overview["B9"].number_format = "DD MMM YYYY"
    overview["B10"].number_format = "DD MMM YYYY"

    header_row = 13

    headings = {
        "weekly": [
            "Day",
            "Date",
            "Late Arrivals",
            "Unique Students"
        ],
        "monthly": [
            "Week",
            "Date Range",
            "Late Arrivals",
            "Unique Students"
        ],
        "yearly": [
            "Month",
            "Year",
            "Late Arrivals",
            "Unique Students"
        ]
    }[report_type]

    for column, heading in enumerate(headings, 1):
        overview.cell(
            header_row,
            column,
            heading
        )

    for row_number, bucket in enumerate(
        buckets,
        header_row + 1
    ):

        label, begin, finish, detail = bucket

        matching = [
            record
            for record in records
            if (
                begin
                <= record["arrival_time"].date()
                < finish
            )
        ]

        unique_count = len({
            excel_student_key(record)
            for record in matching
        })

        values = [
            label,
            detail,
            len(matching),
            unique_count
        ]

        for column, value in enumerate(values, 1):
            overview.cell(
                row_number,
                column,
                value
            )

    last_data_row = header_row + len(buckets)
    total_row = last_data_row + 1

    overview.cell(total_row, 1, "TOTAL")
    overview.cell(total_row, 3, len(records))
    overview.cell(total_row, 4, unique_students)

    excel_format_table(
        overview,
        header_row,
        4,
        total_row
    )

    overview.column_dimensions["A"].width = 25
    overview.column_dimensions["B"].width = 32

    # TREND CHART

    chart = BarChart()
    chart.type = "col"
    chart.style = 10

    chart.title = {
        "weekly": "Daily Late Arrivals",
        "monthly": "Weekly Late Arrivals",
        "yearly": "Monthly Late Arrivals"
    }[report_type]

    chart.y_axis.title = "Late Arrivals"

    chart.add_data(
        Reference(
            overview,
            min_col=3,
            min_row=header_row,
            max_row=last_data_row
        ),
        titles_from_data=True
    )

    chart.set_categories(
        Reference(
            overview,
            min_col=1,
            min_row=header_row + 1,
            max_row=last_data_row
        )
    )

    chart.legend = None
    chart.width = 17
    chart.height = 9

    overview.add_chart(chart, "F13")

    # =================================================
    # SHEET 2: GRADE-WISE BREAKDOWN
    # =================================================

    grades_sheet = workbook.create_sheet(
        "Grade-wise Breakdown"
    )

    excel_sheet_title(
        grades_sheet,
        "Grade-wise Late Arrival Analysis",
        settings["label"]
    )

    grade_header = 7

    grades_sheet.cell(
        grade_header,
        1,
        "Grade"
    )

    for column, bucket in enumerate(buckets, 2):

        label, _, _, _ = bucket

        grades_sheet.cell(
            grade_header,
            column,
            label
        )

    last_col = len(buckets) + 2

    grades_sheet.cell(
        grade_header,
        last_col,
        "TOTAL"
    )

    grades = sorted(
        {
            str(record["grade"])
            for record in records
        },
        key=excel_grade_key
    )

    for row_number, grade in enumerate(
        grades,
        grade_header + 1
    ):

        grades_sheet.cell(
            row_number,
            1,
            grade
        )

        for column, bucket in enumerate(
            buckets,
            2
        ):

            _, begin, finish, _ = bucket

            count = sum(
                1
                for record in records
                if (
                    str(record["grade"]) == grade
                    and begin
                    <= record["arrival_time"].date()
                    < finish
                )
            )

            grades_sheet.cell(
                row_number,
                column,
                count
            )

        grade_total = sum(
            1
            for record in records
            if str(record["grade"]) == grade
        )

        grades_sheet.cell(
            row_number,
            last_col,
            grade_total
        )

    grade_total_row = (
        grade_header + len(grades) + 1
    )

    grades_sheet.cell(
        grade_total_row,
        1,
        "TOTAL"
    )

    for column, bucket in enumerate(buckets, 2):

        _, begin, finish, _ = bucket

        count = sum(
            1
            for record in records
            if (
                begin
                <= record["arrival_time"].date()
                < finish
            )
        )

        grades_sheet.cell(
            grade_total_row,
            column,
            count
        )

    grades_sheet.cell(
        grade_total_row,
        last_col,
        len(records)
    )

    excel_format_table(
        grades_sheet,
        grade_header,
        last_col,
        grade_total_row
    )

    grades_sheet.column_dimensions["A"].width = 20

    # =================================================
    # SHEET 3: STUDENT RECORDS
    # =================================================

    details = workbook.create_sheet(
        "Student Records"
    )

    excel_sheet_title(
        details,
        "Detailed Student Late Arrival Records",
        settings["label"]
    )

    detail_header = 7

    detail_headings = [
        "No.",
        "Student Name",
        "Grade",
        "Section",
        "Date",
        "Day",
        "Arrival Time",
        "Recorded By"
    ]

    for column, heading in enumerate(
        detail_headings,
        1
    ):

        details.cell(
            detail_header,
            column,
            heading
        )

    for index, record in enumerate(records, 1):

        arrival = record["arrival_time"]

        values = [
            index,
            record["student_name"],
            record["grade"],
            record["section"],
            arrival.date(),
            arrival.strftime("%A"),
            arrival.time(),
            record["recorded_by"]
        ]

        row_number = detail_header + index

        for column, value in enumerate(values, 1):

            details.cell(
                row_number,
                column,
                value
            )

        details.cell(
            row_number,
            5
        ).number_format = "DD MMM YYYY"

        details.cell(
            row_number,
            7
        ).number_format = "hh:mm AM/PM"

    excel_format_table(
        details,
        detail_header,
        8
    )

    widths = {
        "A": 10,
        "B": 35,
        "C": 15,
        "D": 15,
        "E": 20,
        "F": 18,
        "G": 20,
        "H": 25
    }

    for column, width in widths.items():
        details.column_dimensions[column].width = width

    # =================================================
    # EXCEL FILE NAME
    # =================================================

    if report_type == "weekly":

        start_date = settings["start"]
        end_date = (
            settings["end"] - timedelta(days=1)
        )

        filename = (
            f"TimeWise_Weekly_{start_date:%d%b%Y}_"
            f"{end_date:%d%b%Y}.xlsx"
        )

    elif report_type == "monthly":

        filename = (
            f"TimeWise_Monthly_"
            f"{settings['start']:%B%Y}.xlsx"
        )

    else:

        filename = (
            f"TimeWise_Yearly_"
            f"{settings['year']}.xlsx"
        )

    # =================================================
    # SAVE AND DOWNLOAD
    # =================================================

    output = BytesIO()

    workbook.save(output)
    output.seek(0)

    return send_file(
        output,
        as_attachment=True,
        download_name=filename,
        mimetype=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        )
    )


# =====================================================
# START FLASK
# =====================================================

if __name__ == "__main__":
    app.run(debug=True)
