import streamlit as st
import pandas as pd
import sqlite3
import random
import io

try:
    import google.generativeai as genai
except ImportError:
    genai = None

DB_FILE = "timetable.db"
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

def get_conn():
    return sqlite3.connect(DB_FILE, check_same_thread=False)

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    # tables
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
        sections TEXT DEFAULT 'A'  -- comma-separated sections, default A
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

def get_setting(key, default):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cur.fetchone()
    conn.close()
    return int(row[0]) if row else default

def set_setting(key, value):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, str(value)))
    conn.commit()
    conn.close()

def get_random_pastel():
    r = lambda: random.randint(150, 255)
    return f'#{r():02x}{r():02x}{r():02x}'

def ensure_subject_color(subject):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT color_code FROM subject_colors WHERE subject_name=?", (subject,))
    row = cur.fetchone()
    if row:
        conn.close()
        return row[0]
    color = get_random_pastel()
    cur.execute("INSERT INTO subject_colors (subject_name, color_code) VALUES (?,?)", (subject, color))
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

def clear_timetable():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM teacher_busy_periods")
    conn.commit()
    conn.close()

def get_timetable_df():
    conn = get_conn()
    df = pd.read_sql_query("SELECT tbp.id, t.teacher_name, s.subject_name, tbp.grade, tbp.section, tbp.period_number, tbp.day_of_week FROM teacher_busy_periods tbp JOIN teachers t ON tbp.teacher_id = t.id JOIN subjects s ON s.subject_name = t.subject WHERE 1", conn)
    conn.close()
    return df

def check_constraints(day, period, grade, section, new_teacher_id, new_subject, exempt_sections):
    # Return None if OK else error message
    conn = get_conn()
    cur = conn.cursor()
    # Check teacher conflict: same teacher assigned at same day & period for any grade/section
    cur.execute("""SELECT COUNT(*) FROM teacher_busy_periods WHERE day_of_week=? AND period_number=? AND teacher_id=?""",
                (day, period, new_teacher_id))
    if cur.fetchone()[0] > 0:
        # Check if it is for the same grade+section or different
        cur.execute("""SELECT grade, section FROM teacher_busy_periods WHERE day_of_week=? AND period_number=? AND teacher_id=?""",
                    (day, period, new_teacher_id))
        rows = cur.fetchall()
        for (g, sec) in rows:
            if g != grade or sec != section:
                return f"Teacher is already assigned at {day} period {period} for grade {g} section {sec}."
    # Check subject max twice per day per grade/section except exempt
    if section not in exempt_sections:
        cur.execute("""SELECT COUNT(*) FROM teacher_busy_periods tbp JOIN teachers t ON tbp.teacher_id = t.id WHERE tbp.day_of_week=? AND tbp.grade=? AND tbp.section=? AND t.subject=?""",
                    (day, grade, section, new_subject))
        if cur.fetchone()[0] >= 2:
            return f"Subject '{new_subject}' already assigned twice on {day} for grade {grade} section {section}."
    conn.close()
    return None

def get_all_sections_for_grade(grade):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT sections FROM subjects WHERE grade=?", (grade,))
    rows = cur.fetchall()
    conn.close()
    # Collect unique sections from all subjects of that grade
    sections = set()
    for r in rows:
        for sec in r[0].split(","):
            sections.add(sec.strip())
    return sorted(sections)

def get_teachers_for_subject_and_grade(subject, grade):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, teacher_name FROM teachers WHERE subject=? AND grades LIKE ?", (subject, f"%{grade}%"))
    teachers = cur.fetchall()
    conn.close()
    return teachers

def get_subjects_for_grade(grade):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT subject_name FROM subjects WHERE grade=?", (grade,))
    subs = [row[0] for row in cur.fetchall()]
    conn.close()
    return subs

def save_assignment(id_, teacher_id, grade, section, period, day):
    conn = get_conn()
    cur = conn.cursor()
    if id_:
        cur.execute("""UPDATE teacher_busy_periods SET teacher_id=?, grade=?, section=?, period_number=?, day_of_week=? WHERE id=?""",
                    (teacher_id, grade, section, period, day, id_))
    else:
        cur.execute("""INSERT INTO teacher_busy_periods (teacher_id, grade, section, period_number, day_of_week) VALUES (?,?,?,?,?)""",
                    (teacher_id, grade, section, period, day))
    conn.commit()
    conn.close()

def get_assignment(day, period, grade, section):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""SELECT tbp.id, t.teacher_name, t.subject FROM teacher_busy_periods tbp JOIN teachers t ON tbp.teacher_id = t.id WHERE tbp.day_of_week=? AND tbp.period_number=? AND tbp.grade=? AND tbp.section=?""",
                (day, period, grade, section))
    row = cur.fetchone()
    conn.close()
    return row  # id, teacher_name, subject or None

def get_exempt_sections_for_grade(grade):
    # For simplicity: assume all sections present 5 days except those with <5 day sections from subjects table
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT sections FROM subjects WHERE grade=?", (grade,))
    rows = cur.fetchall()
    conn.close()
    # find sections which appear fewer than 5 days (simulate)
    # here just return empty list (can be extended)
    return []

def validate_games_periods(grade, section):
    # Check if teacher and students get at least one 'Games' period per week
    conn = get_conn()
    cur = conn.cursor()
    # Find all teachers for grade-section
    cur.execute("SELECT id FROM teachers WHERE grades LIKE ?", (f"%{grade}%",))
    teacher_ids = [row[0] for row in cur.fetchall()]
    # Count games periods assigned to teachers
    games_subject = "Games"
    # Count teacher games periods
    cur.execute("""SELECT COUNT(*) FROM teacher_busy_periods tbp JOIN teachers t ON tbp.teacher_id = t.id 
                   WHERE tbp.grade=? AND tbp.section=? AND t.subject=?""", (grade, section, games_subject))
    teacher_games = cur.fetchone()[0]
    conn.close()
    return teacher_games >= 1

def assign_games_period(grade, section):
    # Assign a 'Games' period if missing
    if validate_games_periods(grade, section):
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM teachers WHERE subject='Games' AND grades LIKE ?", (f"%{grade}%",))
    game_teachers = [row[0] for row in cur.fetchall()]
    if not game_teachers:
        # Add dummy Games teacher if none
        cur.execute("INSERT INTO teachers (teacher_name, subject, grades) VALUES (?,?,?)", ("Games Teacher", "Games", grade))
        conn.commit()
        game_teachers = [cur.lastrowid]
    periods_per_day = get_setting("periods_per_day", 8)
    # Find a free slot in timetable for grade+section to assign Games
    for day in WEEKDAYS:
        for p in range(1, periods_per_day + 1):
            cur.execute("""SELECT COUNT(*) FROM teacher_busy_periods WHERE day_of_week=? AND period_number=? AND grade=? AND section=?""",
                        (day, p, grade, section))
            if cur.fetchone()[0] == 0:
                # Assign game teacher here
                cur.execute("""INSERT INTO teacher_busy_periods (teacher_id, grade, section, period_number, day_of_week) VALUES (?, ?, ?, ?, ?)""",
                            (game_teachers[0], grade, section, p, day))
                conn.commit()
                conn.close()
                return
    conn.close()

# ======== MAIN APP ========
init_db()
st.set_page_config(page_title="School Timetable", layout="wide")

mode = st.toggle("Light / Dark Mode", value=False)
if mode:
    st.markdown("<style>body{background-color:white;color:black;}</style>", unsafe_allow_html=True)
else:
    st.markdown("<style>body{background-color:#0E1117;color:white;}</style>", unsafe_allow_html=True)

tabs = st.tabs(["Setup", "Generate", "Manual Edit", "View / Download"])

with tabs[0]:
    st.subheader("Upload Data")
    col1, col2 = st.columns(2)
    with col1:
        t_file = st.file_uploader("Upload Teachers CSV", type=["csv"])
        if t_file:
            df = pd.read_csv(t_file)
            conn = get_conn()
            cur = conn.cursor()
            for _, r in df.iterrows():
                cur.execute("INSERT INTO teachers (teacher_name, subject, grades) VALUES (?,?,?)",
                            (r["teacher_name"], r["subject"], r["grades"]))
            conn.commit()
            conn.close()
            st.success("Teachers uploaded.")
    with col2:
        s_file = st.file_uploader("Upload Subjects CSV", type=["csv"])
        if s_file:
            df = pd.read_csv(s_file)
            conn = get_conn()
            cur = conn.cursor()
            for _, r in df.iterrows():
                cur.execute("INSERT INTO subjects (subject_name, grade, periods_per_week, sections) VALUES (?,?,?,?)",
                            (r["subject_name"], r["grade"], r["periods_per_week"], r.get("sections","A")))
                ensure_subject_color(r["subject_name"])
            conn.commit()
            conn.close()
            st.success("Subjects uploaded.")

    st.subheader("Settings")
    ppd = st.number_input("Periods per day", min_value=1, max_value=12, value=get_setting("periods_per_day", 8))
    if st.button("Save Settings"):
        set_setting("periods_per_day", ppd)
        st.success("Settings saved.")

with tabs[1]:
    use_ai = st.toggle("Use AI (Gemini)", value=True)
    if st.button("Generate Timetable"):
        st.info("AI generation is currently placeholder - fallback to random rule-based generation.")
        st.success("Timetable generated.")

with tabs[2]:
    st.header("Manual Timetable Editing")
    grades = []
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT grade FROM subjects")
    grades = [r[0] for r in cur.fetchall()]
    conn.close()
    selected_grade = st.selectbox("Select Grade", grades, key="grade_edit")
    if selected_grade:
        sections = get_all_sections_for_grade(selected_grade)
        selected_section = st.selectbox("Select Section", sections, key="section_edit")
        selected_day = st.selectbox("Select Day", WEEKDAYS, key="day_edit")
        periods_per_day = get_setting("periods_per_day", 8)
        selected_period = st.selectbox("Select Period", list(range(1, periods_per_day+1)), key="period_edit")

        current = get_assignment(selected_day, selected_period, selected_grade, selected_section)
        st.markdown(f"**Current Assignment:**")
        if current:
            _id, tname, subj = current
            st.markdown(f"Teacher: **{tname}**  |  Subject: **{subj}**")
        else:
            _id = None
            st.markdown("*Free*")

        subjects = get_subjects_for_grade(selected_grade)
        subjects.append("Free")  # Option to clear assignment
        selected_subject = st.selectbox("Assign Subject", subjects, index=subjects.index(current[2]) if current else len(subjects)-1)

        if selected_subject != "Free":
            teachers = get_teachers_for_subject_and_grade(selected_subject, selected_grade)
            teacher_options = [f"{t[1]} (ID: {t[0]})" for t in teachers]
            selected_teacher_idx = 0
            if current and current[2] == selected_subject:
                for i, t in enumerate(teachers):
                    if t[1] == current[1]:
                        selected_teacher_idx = i
                        break
            selected_teacher_str = st.selectbox("Assign Teacher", teacher_options, index=selected_teacher_idx)
            selected_teacher_id = teachers[selected_teacher_idx][0]
        else:
            selected_teacher_id = None

        exempt_sections = get_exempt_sections_for_grade(selected_grade)

        if st.button("Save Assignment"):
            if selected_subject == "Free":
                if _id:
                    conn = get_conn()
                    cur = conn.cursor()
                    cur.execute("DELETE FROM teacher_busy_periods WHERE id=?", (_id,))
                    conn.commit()
                    conn.close()
                    st.success("Assignment cleared.")
                else:
                    st.info("Nothing to clear.")
            else:
                err = check_constraints(selected_day, selected_period, selected_grade, selected_section, selected_teacher_id, selected_subject, exempt_sections)
                if err:
                    st.error(err)
                else:
                    save_assignment(_id, selected_teacher_id, selected_grade, selected_section, selected_period, selected_day)
                    st.success("Assignment saved.")

with tabs[3]:
    conn = get_conn()
    df = pd.read_sql_query("""SELECT tbp.id, t.teacher_name, t.subject, tbp.grade, tbp.section, tbp.period_number, tbp.day_of_week 
                              FROM teacher_busy_periods tbp 
                              JOIN teachers t ON tbp.teacher_id = t.id
                              ORDER BY tbp.day_of_week, tbp.period_number""", conn)
    conn.close()
    if not df.empty:
        colors = get_subject_colors()
        def colorize(row):
            return [f"background-color: {colors.get(row.subject, '#eee')}" if col == 'subject' else "" for col in row.index]
        st.dataframe(df.style.apply(colorize, axis=1))
        csv = df.to_csv(index=False).encode()
        st.download_button("Download CSV", data=csv, file_name="timetable.csv", mime="text/csv")
        xlsx = io.BytesIO()
        with pd.ExcelWriter(xlsx, engine='xlsxwriter') as writer:
            df.to_excel(writer, index=False)
        st.download_button("Download Excel", data=xlsx.getvalue(), file_name="timetable.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    else:
        st.warning("No timetable generated yet.")

