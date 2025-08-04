# ==============================
# SCHOOL TIMETABLE GENERATOR - FULL VERSION
# ==============================

import streamlit as st
import pandas as pd
import sqlite3
import random
import os
import google.generativeai as genai

# ==============================
# CONFIG
# ==============================

DB_FILE = "timetable.db"

# Configure Gemini AI (Google Generative AI)
if "GEMINI_API_KEY" in st.secrets:
    genai.configure(api_key=st.secrets["GEMINI_API_KEY"])
else:
    st.warning("No GEMINI_API_KEY found in secrets. AI features will be disabled.")
    genai = None

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

# ==============================
# DB CONNECTION & INIT
# ==============================

def get_conn():
    return sqlite3.connect(DB_FILE, check_same_thread=False)

def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""CREATE TABLE IF NOT EXISTS teachers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        teacher_name TEXT,
        subject TEXT,
        grades TEXT
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS subjects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subject_name TEXT,
        grade TEXT,
        periods_per_week INTEGER,
        sections TEXT DEFAULT 'A'
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS subject_colors (
        subject_name TEXT PRIMARY KEY,
        color_code TEXT
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS teacher_busy_periods (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        teacher_id INTEGER,
        grade TEXT,
        section TEXT,
        period_number INTEGER,
        day_of_week TEXT
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )""")

    conn.commit()
    conn.close()

# ==============================
# COLOR UTILS
# ==============================

def get_random_pastel():
    r = lambda: random.randint(150, 255)
    return f'#{r():02x}{r():02x}{r():02x}'

def get_contrasting_text_color(hex_color):
    hex_color = hex_color.lstrip('#')
    r, g, b = [int(hex_color[i:i+2], 16) for i in (0, 2, 4)]
    brightness = (r*299 + g*587 + b*114) / 1000
    return '#000000' if brightness > 150 else '#FFFFFF'

def ensure_subject_color(subject_name):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT color_code FROM subject_colors WHERE subject_name=?", (subject_name,))
    row = cur.fetchone()
    if row:
        conn.close()
        return row[0]
    color = get_random_pastel()
    cur.execute("INSERT INTO subject_colors (subject_name, color_code) VALUES (?, ?)",
                (subject_name, color))
    conn.commit()
    conn.close()
    return color

def get_subject_colors():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT subject_name, color_code FROM subject_colors")
    colors = {name: code for name, code in cur.fetchall()}
    conn.close()
    return colors

# ==============================
# AI GENERATION
# ==============================

def generate_timetable_ai(prompt):
    if genai is None:
        st.error("AI is disabled because GEMINI_API_KEY is missing.")
        return None
    model = genai.GenerativeModel("gemini-pro")
    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        st.error(f"AI Error: {e}")
        return None

# ==============================
# DB HELPER FUNCTIONS
# ==============================

def add_teacher(teacher_name, subject, grades):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO teachers (teacher_name, subject, grades) VALUES (?, ?, ?)",
                (teacher_name, subject, grades))
    conn.commit()
    conn.close()

def add_subject(subject_name, grade, periods_per_week, sections):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""INSERT INTO subjects (subject_name, grade, periods_per_week, sections)
                   VALUES (?, ?, ?, ?)""",
                (subject_name, grade, periods_per_week, sections))
    conn.commit()
    conn.close()
    ensure_subject_color(subject_name)

def get_all_teachers():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT teacher_name, subject, grades FROM teachers")
    teachers = cur.fetchall()
    conn.close()
    return teachers

def get_all_subjects():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT subject_name, grade, periods_per_week, sections FROM subjects")
    subs = cur.fetchall()
    conn.close()
    return subs

def clear_timetable():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM teacher_busy_periods")
    conn.commit()
    conn.close()
# ==============================
# TIMETABLE GENERATION LOGIC
# ==============================

def is_teacher_busy(teacher_id, day, period):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""SELECT COUNT(*) FROM teacher_busy_periods
                   WHERE teacher_id=? AND day_of_week=? AND period_number=?""",
                (teacher_id, day, period))
    busy = cur.fetchone()[0] > 0
    conn.close()
    return busy

def assign_period(teacher_id, grade, section, day, period):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""INSERT INTO teacher_busy_periods
                   (teacher_id, grade, section, day_of_week, period_number)
                   VALUES (?, ?, ?, ?, ?)""",
                (teacher_id, grade, section, day, period))
    conn.commit()
    conn.close()

def generate_timetable(periods_per_day=7):
    clear_timetable()
    teachers = get_all_teachers()
    subjects = get_all_subjects()

    conn = get_conn()
    cur = conn.cursor()
    colors = get_subject_colors()

    # Create subject -> teacher mapping for quick lookup
    teacher_map = {}
    for tidx, (tname, subj, grades) in enumerate(teachers, start=1):
        for g in grades.split(","):
            g = g.strip()
            teacher_map.setdefault((g, subj), []).append((tidx, tname))

    # Generate timetable
    for subj_name, grade, ppw, sections in subjects:
        sections_list = [s.strip() for s in sections.split(",")]
        for section in sections_list:
            assigned_periods = 0
            days_shuffled = WEEKDAYS.copy()
            random.shuffle(days_shuffled)

            for day in days_shuffled:
                if assigned_periods >= ppw:
                    break
                periods_today = 0
                for period in range(1, periods_per_day + 1):
                    if assigned_periods >= ppw:
                        break

                    # Constraint: Max 2 periods per day per subject (unless section attends < 5 days)
                    if periods_today >= 2 and len(days_shuffled) == 5:
                        break

                    # Pick a teacher
                    if (grade, subj_name) not in teacher_map:
                        teacher_id = None
                    else:
                        possible_teachers = teacher_map[(grade, subj_name)]
                        random.shuffle(possible_teachers)
                        teacher_id = None
                        for tid, _ in possible_teachers:
                            if not is_teacher_busy(tid, day, period):
                                teacher_id = tid
                                break

                    # If no teacher is available, substitution
                    if teacher_id is None:
                        # Try games teacher
                        if (grade, "Games") in teacher_map:
                            tid, _ = random.choice(teacher_map[(grade, "Games")])
                            if not is_teacher_busy(tid, day, period):
                                teacher_id = tid
                                subj_display = "Games"
                            else:
                                teacher_id = None
                        else:
                            subj_display = "Library"
                            tid = None
                            teacher_id = None
                    else:
                        subj_display = subj_name

                    if teacher_id is not None or subj_display == "Library":
                        assign_period(teacher_id if teacher_id else -1, grade, section, day, period)
                        assigned_periods += 1
                        periods_today += 1

    conn.close()

# ==============================
# AI HELPERS
# ==============================

def suggest_changes_with_ai(timetable_df):
    """Use Gemini AI to suggest timetable improvements."""
    csv_data = timetable_df.to_csv(index=False)
    prompt = f"""You are an expert school timetable planner.
    Here is the current timetable in CSV:
    {csv_data}

    Suggest improvements considering:
    - No teacher teaches two places at once
    - Students have balanced subjects
    - Games at least once a week for all
    - Max 2 periods per subject per day unless less than 5 days attendance
    """

    return generate_timetable_ai(prompt)

# ==============================
# DISPLAY HELPERS
# ==============================

def display_timetable(grade, section):
    conn = get_conn()
    query = """
    SELECT tbp.day_of_week, tbp.period_number, t.subject, t.teacher_name
    FROM teacher_busy_periods tbp
    LEFT JOIN teachers t ON tbp.teacher_id = t.id
    WHERE tbp.grade=? AND tbp.section=?
    ORDER BY tbp.day_of_week, tbp.period_number
    """
    df = pd.read_sql_query(query, conn, params=(grade, section))
    conn.close()

    if df.empty:
        st.warning("No timetable found for this grade/section.")
        return

    # Pivot for display
    timetable_pivot = df.pivot(index="period_number", columns="day_of_week", values="subject").fillna("")
    st.dataframe(timetable_pivot)

    if st.checkbox("Suggest AI Improvements"):
        suggestion = suggest_changes_with_ai(df)
        if suggestion:
            st.markdown("### AI Suggestions")
            st.write(suggestion)
# ==============================
# STREAMLIT UI
# ==============================
st.set_page_config(page_title="School Timetable Generator", layout="wide")

st.title("📅 School Timetable Generator")

init_db()

menu = st.sidebar.radio("Menu", [
    "Upload Data", "Generate Timetable", "View Timetable", "Manual Edit"
])

# ------------------------------
# UPLOAD DATA (CSV + Manual)
# ------------------------------
if menu == "Upload Data":
    st.header("Upload or Enter Teacher & Subject Data")

    tab1, tab2 = st.tabs(["📄 CSV Upload", "✍ Manual Entry"])

    with tab1:
        teacher_file = st.file_uploader("Upload Teachers CSV", type=["csv"], key="teacher_csv")
        if teacher_file:
            df_t = pd.read_csv(teacher_file)
            conn = get_conn()
            df_t.to_sql("teachers", conn, if_exists="append", index=False)
            conn.close()
            st.success("✅ Teachers uploaded.")

        subject_file = st.file_uploader("Upload Subjects CSV", type=["csv"], key="subject_csv")
        if subject_file:
            df_s = pd.read_csv(subject_file)
            conn = get_conn()
            df_s.to_sql("subjects", conn, if_exists="append", index=False)
            conn.close()
            st.success("✅ Subjects uploaded.")

    with tab2:
        with st.form("teacher_form"):
            t_name = st.text_input("Teacher Name")
            t_subject = st.text_input("Subject")
            t_grades = st.text_input("Grades (comma separated)")
            submitted = st.form_submit_button("Add Teacher")
            if submitted:
                add_teacher(t_name, t_subject, t_grades)
                st.success("Teacher added.")

        with st.form("subject_form"):
            s_name = st.text_input("Subject Name")
            s_grade = st.text_input("Grade")
            s_ppw = st.number_input("Periods per Week", min_value=1, value=5)
            s_sections = st.text_input("Sections (comma separated)", value="A")
            submitted_s = st.form_submit_button("Add Subject")
            if submitted_s:
                add_subject(s_name, s_grade, s_ppw, s_sections)
                st.success("Subject added.")

# ------------------------------
# GENERATE TIMETABLE
# ------------------------------
elif menu == "Generate Timetable":
    st.header("Generate Timetable")
    periods = st.number_input("Number of periods per day", min_value=1, value=7)
    if st.button("Generate Now"):
        generate_timetable(periods_per_day=periods)
        st.success("✅ Timetable generated successfully.")

# ------------------------------
# VIEW TIMETABLE
# ------------------------------
elif menu == "View Timetable":
    st.header("View Timetable")
    grades = get_all_grades()
    grade = st.selectbox("Select Grade", grades, key="view_grade")
    sections = get_sections_for_grade(grade)
    section = st.selectbox("Select Section", sections, key="view_section")

    if st.button("Show Timetable"):
        display_timetable(grade, section)

        # Download option
        conn = get_conn()
        df = pd.read_sql_query(
            "SELECT * FROM teacher_busy_periods WHERE grade=? AND section=?",
            conn, params=(grade, section)
        )
        conn.close()
        csv = df.to_csv(index=False).encode('utf-8')
        st.download_button("⬇ Download CSV", csv, file_name=f"{grade}_{section}_timetable.csv")

# ------------------------------
# MANUAL EDIT
# ------------------------------
elif menu == "Manual Edit":
    st.header("Manual Timetable Editing (with Constraints)")
    grades = get_all_grades()
    grade = st.selectbox("Grade", grades, key="edit_grade")
    sections = get_sections_for_grade(grade)
    section = st.selectbox("Section", sections, key="edit_section")

    conn = get_conn()
    df = pd.read_sql_query("""
        SELECT tbp.id, tbp.day_of_week, tbp.period_number, t.subject, t.teacher_name
        FROM teacher_busy_periods tbp
        LEFT JOIN teachers t ON tbp.teacher_id = t.id
        WHERE tbp.grade=? AND tbp.section=?
    """, conn, params=(grade, section))
    conn.close()

    if not df.empty:
        row_to_edit = st.selectbox("Select Period to Edit", df.index)
        new_subject = st.text_input("New Subject", value=df.loc[row_to_edit, "subject"])
        new_teacher = st.text_input("New Teacher", value=df.loc[row_to_edit, "teacher_name"])
        if st.button("Update Period"):
            # Check constraints before updating
            teacher_id = get_teacher_id_by_name(new_teacher)
            if not is_teacher_busy(teacher_id, df.loc[row_to_edit, "day_of_week"], df.loc[row_to_edit, "period_number"]):
                update_period(df.loc[row_to_edit, "id"], teacher_id, new_subject)
                st.success("✅ Period updated successfully.")
            else:
                st.error("❌ Constraint violated: Teacher already busy in that period.")
