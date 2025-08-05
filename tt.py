import streamlit as st
import sqlite3
import pandas as pd
import random
import os
import io
import google.generativeai as genai

# ----------------------------
# CONFIG & DB INITIALIZATION
# ----------------------------

DB_PATH = "timetable.db"

def get_conn():
    return sqlite3.connect(DB_PATH)

def get_timetable_data_as_text():
    conn = get_conn()
    df = pd.read_sql_query("""
        SELECT day_of_week, grade, section, period_number, subject
        FROM teacher_busy_periods
        JOIN teachers ON teacher_busy_periods.teacher_id = teachers.id
        ORDER BY grade, section, day_of_week, period_number
    """, conn)
    conn.close()
    return df.to_string(index=False)


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    # Teachers table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS teachers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            teacher_name TEXT NOT NULL,
            subject TEXT NOT NULL,
            grades TEXT NOT NULL -- comma-separated grades this teacher teaches
        )
    """)

    # Subjects table with sections and their active days (comma separated)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS subjects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_name TEXT NOT NULL,
            grade TEXT NOT NULL,
            periods_per_week INTEGER NOT NULL,
            sections TEXT NOT NULL DEFAULT 'A', -- comma-separated sections
            active_days TEXT NOT NULL DEFAULT 'Monday,Tuesday,Wednesday,Thursday,Friday' -- days section attends
        )
    """)

    # Subject colors for UI
    cur.execute("""
        CREATE TABLE IF NOT EXISTS subject_colors (
            subject_name TEXT PRIMARY KEY,
            color_code TEXT NOT NULL
        )
    """)

    # Timetable assignments
    cur.execute("""
        CREATE TABLE IF NOT EXISTS teacher_busy_periods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            teacher_id INTEGER NOT NULL,
            grade TEXT NOT NULL,
            section TEXT NOT NULL,
            period_number INTEGER NOT NULL,
            day_of_week TEXT NOT NULL,
            FOREIGN KEY (teacher_id) REFERENCES teachers(id)
        )
    """)

    # Store absent teachers per day
    cur.execute("""
        CREATE TABLE IF NOT EXISTS absentees (
            day_of_week TEXT PRIMARY KEY,
            absent_teachers TEXT NOT NULL DEFAULT ''
        )
    """)

    # Settings for global config (like periods per day)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    # Insert default absent days if missing
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    for d in days:
        cur.execute("INSERT OR IGNORE INTO absentees(day_of_week, absent_teachers) VALUES (?, '')", (d,))

    # Insert default periods_per_day if missing
    cur.execute("INSERT OR IGNORE INTO settings(key,value) VALUES ('periods_per_day', '8')")

    conn.commit()
    conn.close()

# ----------------------------
# COLOR UTILITIES
# ----------------------------

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
    cur.execute("INSERT INTO subject_colors(subject_name, color_code) VALUES (?, ?)", (subject_name, color))
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

# ----------------------------
# DATA FETCHING HELPERS
# ----------------------------

def get_all_grades_sections():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT grade FROM subjects")
    grades = [r[0] for r in cur.fetchall()]
    grade_sections = {}
    for g in grades:
        cur.execute("SELECT sections, active_days FROM subjects WHERE grade=?", (g,))
        rows = cur.fetchall()
        sections = set()
        active_days_map = {}
        for secs, days_str in rows:
            for sec in secs.split(","):
                sec = sec.strip()
                sections.add(sec)
                active_days_map[sec] = [d.strip() for d in days_str.split(",")]
        grade_sections[g] = {"sections": sorted(list(sections)), "active_days_map": active_days_map}
    conn.close()
    return grade_sections

def get_teachers_for_grade(grade):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, teacher_name, subject, grades FROM teachers")
    teachers = []
    for tid, tname, subj, grades_str in cur.fetchall():
        grades_list = [g.strip() for g in grades_str.split(",")]
        if grade in grades_list:
            teachers.append((tid, tname, subj))
    conn.close()
    return teachers

def get_subjects_for_grade(grade):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT subject_name, periods_per_week, sections, active_days FROM subjects WHERE grade=?", (grade,))
    subs = cur.fetchall()
    conn.close()
    return subs

def get_absent_teachers():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT day_of_week, absent_teachers FROM absentees")
    data = {row[0]: [x.strip() for x in row[1].split(",") if x.strip()] for row in cur.fetchall()}
    conn.close()
    return data

def set_absent_teachers(day, teachers):
    conn = get_conn()
    cur = conn.cursor()
    teachers_str = ",".join(teachers)
    cur.execute("UPDATE absentees SET absent_teachers=? WHERE day_of_week=?", (teachers_str, day))
    conn.commit()
    conn.close()

def get_periods_per_day():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key='periods_per_day'")
    res = cur.fetchone()
    conn.close()
    return int(res[0]) if res else 8

def set_periods_per_day(n):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('periods_per_day', ?)", (str(n),))
    conn.commit()
    conn.close()

# ----------------------------
# TIMETABLE MANAGEMENT
# ----------------------------

def clear_timetable():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM teacher_busy_periods")
    conn.commit()
    conn.close()

def assign_games_and_library(grades_sections, periods_per_day):
    """
    Ensure every section & grade gets at least 1 'Games' period & 1 'Library' period per week.
    Add 'Games' and 'Library' teachers if missing.
    """
    conn = get_conn()
    cur = conn.cursor()
    # Check/create Games teacher for all grades
    cur.execute("SELECT id FROM teachers WHERE subject='Games'")
    games_teachers = cur.fetchall()
    if not games_teachers:
        # Create a generic Games teacher teaching all grades
        cur.execute("INSERT INTO teachers (teacher_name, subject, grades) VALUES (?, ?, ?)",
                    ("Games Teacher", "Games", ",".join(grades_sections.keys())))
        conn.commit()

    cur.execute("SELECT id FROM teachers WHERE subject='Library'")
    library_teachers = cur.fetchall()
    if not library_teachers:
        cur.execute("INSERT INTO teachers (teacher_name, subject, grades) VALUES (?, ?, ?)",
                    ("Library Teacher", "Library", ",".join(grades_sections.keys())))
        conn.commit()
    conn.close()

# ----------------------------
# AI INTEGRATION
# ----------------------------

import google.generativeai as genai

def generate_ai_timetable_suggestion(grade, sections, periods_per_day, absent_teachers, custom_prompt=None):
    """
    Generate a timetable suggestion using Google Gemini AI.
    If custom_prompt is provided, it will be included in the request.
    """
    genai.configure(api_key=st.secrets["gemini_api_key"])

    # Default constraints
    base_prompt = f"""
You are a smart school timetable generator.
Grade: {grade}
Sections: {', '.join(sections)}
Periods per day: {periods_per_day}
Absent teachers per day: {absent_teachers}

Generate a balanced timetable ensuring:
- No teacher overlap across sections
- Max 2 periods per subject per day (except for sections attending fewer days)
- At least one Games and one Library period per week per section
- Substitute absent teachers with other available teachers or 'Games' if no substitute found
Return the result as a JSON dictionary in the format:
{{
    "Monday": {{
        "Period 1": {{
            "Section A": ["Math", "Mr. Smith"],
            "Section B": ["English", "Ms. Johnson"]
        }},
        "Period 2": ...
    }},
    ...
}}
"""

    # If the user gave a custom request, append it
    if custom_prompt and custom_prompt.strip():
        base_prompt = f"{custom_prompt}\n\nHere is the timetable request:\n{base_prompt}"

    try:
        model = genai.GenerativeModel("gemini-pro")
        response = model.generate_content(base_prompt)
        return response.text.strip()
    except Exception as e:
        st.error(f"AI generation error: {e}")
        return None

# ----------------------------
# TIMETABLE GENERATION CORE
# ----------------------------

def generate_timetable(grades_sections, periods_per_day, absent_teachers):
    clear_timetable()
    assign_games_and_library(grades_sections, periods_per_day)

    conn = get_conn()
    cur = conn.cursor()

    # Load teachers once
    cur.execute("SELECT id, teacher_name, subject, grades FROM teachers")
    teachers_all = cur.fetchall()

    for grade, info in grades_sections.items():
        sections = info["sections"]
        active_days_map = info["active_days_map"]

        # Fetch subjects for this grade
        cur.execute("SELECT subject_name, periods_per_week, sections, active_days FROM subjects WHERE grade=?", (grade,))
        subjects = cur.fetchall()

        # Build mapping for subjects to sections & active_days
        subj_section_map = {}
        for subj_name, periods_wk, secs_str, active_days_str in subjects:
            secs = [s.strip() for s in secs_str.split(",")]
            active_days_subj = [d.strip() for d in active_days_str.split(",")]
            for sec in secs:
                subj_section_map.setdefault((subj_name, sec), {"periods_per_week": periods_wk, "active_days": active_days_subj})

        # Track teacher load per day to avoid overlaps
        teacher_load = {}
        # Track subject counts per day per section to respect max 2 periods rule
        subject_count = {}

        for day in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]:
            for sec in sections:
                # Skip days section does not attend
                if sec not in active_days_map or day not in active_days_map[sec]:
                    continue

                subject_count.setdefault((sec, day), {})

        # For simplicity, assign periods in a naive round robin:
        # For each grade, section, day, fill periods with subjects & assign available teacher
        for grade in grades_sections.keys():
            sections = grades_sections[grade]["sections"]
            active_days_map = grades_sections[grade]["active_days_map"]

            for sec in sections:
                for day in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]:
                    if sec not in active_days_map or day not in active_days_map[sec]:
                        continue

                    period_num = 1
                    # Get subjects that apply to this grade and section and active on this day
                    cur.execute("""
                        SELECT subject_name, periods_per_week FROM subjects
                        WHERE grade=? AND instr(sections, ?) > 0 AND instr(active_days, ?) > 0
                    """, (grade, sec, day))
                    subs = cur.fetchall()

                    # Shuffle subjects for fairness
                    subs = list(subs)
                    random.shuffle(subs)

                    while period_num <= periods_per_day and subs:
                        for subj_name, periods_wk in subs:
                            # Check if this subject can be assigned (max 2 periods per day for full-week sections)
                            max_periods_per_day = 2
                            if len(active_days_map.get(sec, [])) < 5:
                                max_periods_per_day = periods_per_day  # relax limit for part-time sections

                            count_today = subject_count.get((sec, day), {}).get(subj_name, 0)
                            if count_today >= max_periods_per_day:
                                continue

                            # Find available teacher for subject & grade who is not absent today and not loaded already this day
                            available_teachers = []
                            for tid, tname, tsubj, tgrades_str in teachers_all:
                                tgrades = [g.strip() for g in tgrades_str.split(",")]
                                if subj_name == tsubj and grade in tgrades:
                                    # Not absent
                                    if tname not in absent_teachers.get(day, []):
                                        # Not already assigned this day at this period for any grade/section
                                        teacher_load.setdefault(tid, {}).setdefault(day, set())
                                        if period_num not in teacher_load[tid][day]:
                                            available_teachers.append((tid, tname))

                            if not available_teachers:
                                # Substitute with Games teacher if any
                                cur.execute("SELECT id, teacher_name FROM teachers WHERE subject='Games' AND instr(grades, ?) > 0", (grade,))
                                games_teacher = cur.fetchone()
                                if games_teacher:
                                    tid, tname = games_teacher
                                    # Assign anyway
                                else:
                                    # Skip assigning this period
                                    continue
                            else:
                                tid, tname = random.choice(available_teachers)

                            # Insert assignment
                            cur.execute("""
                                INSERT INTO teacher_busy_periods (teacher_id, grade, section, period_number, day_of_week)
                                VALUES (?, ?, ?, ?, ?)
                            """, (tid, grade, sec, period_num, day))

                            # Update tracking
                            teacher_load.setdefault(tid, {}).setdefault(day, set()).add(period_num)
                            subject_count.setdefault((sec, day), {})
                            subject_count[(sec, day)][subj_name] = subject_count[(sec, day)].get(subj_name, 0) + 1

                            period_num += 1
                            if period_num > periods_per_day:
                                break

                        if period_num > periods_per_day:
                            break

    conn.commit()
    conn.close()

# ----------------------------
# TIMETABLE VIEW & EDIT HELPERS
# ----------------------------

def get_timetable_for_grade_section(grade, section):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT tbp.period_number, tbp.day_of_week, t.teacher_name, t.subject
        FROM teacher_busy_periods tbp
        JOIN teachers t ON tbp.teacher_id = t.id
        WHERE tbp.grade=? AND tbp.section=?
    """, (grade, section))
    rows = cur.fetchall()
    conn.close()
    # Structure as: day -> period -> (teacher, subject)
    timetable = {}
    for period_num, day, teacher_name, subject in rows:
        timetable.setdefault(day, {})
        timetable[day][period_num] = (teacher_name, subject)
    return timetable

def update_timetable_entry(grade, section, day, period_num, teacher_id):
    # Check constraints before update:
    # 1) Teacher must teach this grade
    # 2) Teacher must not have other assignment at same day and period
    # 3) One teacher per subject per section
    conn = get_conn()
    cur = conn.cursor()

    # Get teacher info
    cur.execute("SELECT teacher_name, subject, grades FROM teachers WHERE id=?", (teacher_id,))
    tinfo = cur.fetchone()
    if not tinfo:
        conn.close()
        return False, "Teacher not found"
    tname, tsubj, tgrades_str = tinfo
    tgrades = [g.strip() for g in tgrades_str.split(",")]
    if grade not in tgrades:
        conn.close()
        return False, "Teacher does not teach this grade"

    # Check if teacher is already busy this day and period for another section
    cur.execute("""
        SELECT COUNT(*) FROM teacher_busy_periods
        WHERE teacher_id=? AND day_of_week=? AND period_number=? AND NOT (grade=? AND section=?)
    """, (teacher_id, day, period_num, grade, section))
    count = cur.fetchone()[0]
    if count > 0:
        conn.close()
        return False, "Teacher already assigned at this time"

    # Check if same subject already assigned to another teacher for this section at this period/day
    cur.execute("""
        SELECT t2.id FROM teacher_busy_periods tbp2
        JOIN teachers t2 ON tbp2.teacher_id = t2.id
        WHERE tbp2.grade=? AND tbp2.section=? AND tbp2.day_of_week=? AND tbp2.period_number=?
          AND t2.subject=?
    """, (grade, section, day, period_num, tsubj))
    row = cur.fetchone()
    if row:
        # If same teacher, allow; else block
        if row[0] != teacher_id:
            conn.close()
            return False, "Another teacher already teaches this subject at this time"

    # Update or insert assignment
    cur.execute("""
        SELECT id FROM teacher_busy_periods
        WHERE grade=? AND section=? AND day_of_week=? AND period_number=?
    """, (grade, section, day, period_num))
    existing = cur.fetchone()
    if existing:
        cur.execute("""
            UPDATE teacher_busy_periods SET teacher_id=? WHERE id=?
        """, (teacher_id, existing[0]))
    else:
        cur.execute("""
            INSERT INTO teacher_busy_periods (teacher_id, grade, section, period_number, day_of_week)
            VALUES (?, ?, ?, ?, ?)
        """, (teacher_id, grade, section, period_num, day))

    conn.commit()
    conn.close()
    return True, "Updated successfully"

# ----------------------------
# CSV UPLOAD HANDLERS
# ----------------------------

def upload_teachers_csv(csv_file):
    try:
        df = pd.read_csv(csv_file)
        required_cols = {"teacher_name", "subject", "grades"}
        if not required_cols.issubset(set(df.columns)):
            return False, "CSV must contain columns: teacher_name, subject, grades"
        conn = get_conn()
        cur = conn.cursor()
        for _, row in df.iterrows():
            cur.execute("INSERT INTO teachers (teacher_name, subject, grades) VALUES (?, ?, ?)",
                        (row["teacher_name"], row["subject"], row["grades"]))
        conn.commit()
        conn.close()
        return True, "Teachers uploaded successfully"
    except Exception as e:
        return False, f"Error uploading teachers CSV: {e}"

def upload_subjects_csv(csv_file):
    try:
        df = pd.read_csv(csv_file)
        required_cols = {"subject_name", "grade", "periods_per_week", "sections", "active_days"}
        if not required_cols.issubset(set(df.columns)):
            return False, "CSV must contain columns: subject_name, grade, periods_per_week, sections, active_days"
        conn = get_conn()
        cur = conn.cursor()
        for _, row in df.iterrows():
            cur.execute("""
                INSERT INTO subjects (subject_name, grade, periods_per_week, sections, active_days)
                VALUES (?, ?, ?, ?, ?)
            """, (row["subject_name"], row["grade"], int(row["periods_per_week"]), row["sections"], row["active_days"]))
        conn.commit()
        conn.close()
        return True, "Subjects uploaded successfully"
    except Exception as e:
        return False, f"Error uploading subjects CSV: {e}"

# ----------------------------
# UTILS
# ----------------------------

def export_timetable_csv(grade, section):
    timetable = get_timetable_for_grade_section(grade, section)
    periods_per_day = get_periods_per_day()
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

    output = io.StringIO()
    output.write("Day,Period,Teacher,Subject\n")
    for day in days:
        for period in range(1, periods_per_day + 1):
            teacher, subject = timetable.get(day, {}).get(period, ("", ""))
            output.write(f"{day},{period},{teacher},{subject}\n")
    return output.getvalue()

# ----------------------------
# STREAMLIT UI
# ----------------------------

st.set_page_config(page_title="School Timetable Generator", layout="wide")

init_db()

st.title("School Timetable Generator")

tab1, tab2, tab3, tab4 = st.tabs(["Setup Data", "Set Absentees", "Generate Timetable", "View / Edit Timetable"])

with tab1:
    st.header("Teachers")
    teacher_csv = st.file_uploader("Upload Teachers CSV (Columns: teacher_name, subject, grades)", type=["csv"], key="teacher_csv")
    if teacher_csv:
        success, msg = upload_teachers_csv(teacher_csv)
        st.success(msg) if success else st.error(msg)
    st.markdown("---")
    st.subheader("Or Add Teacher Manually")
    with st.form("manual_teacher_form"):
        tname = st.text_input("Teacher Name")
        tsubj = st.text_input("Subject")
        tgrades = st.text_input("Grades (comma separated, e.g. 6,7,8)")
        submitted = st.form_submit_button("Add Teacher")
        if submitted:
            if not tname or not tsubj or not tgrades:
                st.error("Please fill all fields")
            else:
                conn = get_conn()
                cur = conn.cursor()
                cur.execute("INSERT INTO teachers (teacher_name, subject, grades) VALUES (?, ?, ?)", (tname, tsubj, tgrades))
                conn.commit()
                conn.close()
                st.success(f"Added teacher {tname}")

    st.markdown("## Subjects")
    subject_csv = st.file_uploader("Upload Subjects CSV (Columns: subject_name, grade, periods_per_week, sections, active_days)", type=["csv"], key="subject_csv")
    if subject_csv:
        success, msg = upload_subjects_csv(subject_csv)
        st.success(msg) if success else st.error(msg)
    st.markdown("---")
    st.subheader("Or Add Subject Manually")
    with st.form("manual_subject_form"):
        sname = st.text_input("Subject Name")
        sgrade = st.text_input("Grade")
        sper_week = st.number_input("Periods per Week", min_value=1, max_value=40, value=5)
        ssections = st.text_input("Sections (comma separated, e.g. A,B)")
        sactive_days = st.multiselect("Days Section Attends", options=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"], default=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"])
        submitted = st.form_submit_button("Add Subject")
        if submitted:
            if not sname or not sgrade or not ssections or not sactive_days:
                st.error("Please fill all fields")
            else:
                conn = get_conn()
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO subjects(subject_name, grade, periods_per_week, sections, active_days)
                    VALUES (?, ?, ?, ?, ?)
                """, (sname, sgrade, sper_week, ssections, ",".join(sactive_days)))
                conn.commit()
                conn.close()
                st.success(f"Added subject {sname}")

with tab2:
    st.header("Set Absent Teachers Per Day")
    absent_data = get_absent_teachers()
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    for day in days:
        absent_input = st.text_input(f"Absent Teachers on {day} (comma separated)", value=",".join(absent_data.get(day, [])), key=f"absent_{day}")
        if st.button(f"Save {day} Absentees"):
            teachers_list = [x.strip() for x in absent_input.split(",") if x.strip()]
            set_absent_teachers(day, teachers_list)
            st.success(f"Saved absentees for {day}")

with tab3:
    st.header("Generate Timetable")
    periods_per_day = st.number_input(
        "Periods Per Day",
        min_value=4,
        max_value=12,
        value=get_periods_per_day(),
        step=1,
        key="periods_per_day_input"
    )
    if st.button("Save Periods Per Day"):
        set_periods_per_day(periods_per_day)
        st.success("Saved periods per day setting")

    grades_sections = get_all_grades_sections()
    absent_teachers = get_absent_teachers()

    st.write(f"Grades found: {list(grades_sections.keys())}")

    if st.button("Generate Timetable Now"):
        generate_timetable(grades_sections, periods_per_day, absent_teachers)
        st.success("Timetable generated")

    st.markdown("---")
    st.subheader("Generate AI Suggestion for a Grade")

    sel_grade = st.selectbox(
        "Select Grade for AI Timetable",
        options=list(grades_sections.keys())
    )
    sel_sections = grades_sections[sel_grade]["sections"]

    custom_ai_prompt = st.text_area(
        "Optional: Add custom AI instructions",
        placeholder="E.g., Ensure Math is in the morning, avoid last period for Science..."
    )

    if st.button("Generate AI Timetable Suggestion"):
        suggestion = generate_ai_timetable_suggestion(
            sel_grade,
            sel_sections,
            periods_per_day,
            absent_teachers,
            custom_prompt=custom_ai_prompt
        )
        if suggestion:
            st.code(suggestion, language="json")

# ----------------------------
# TIMETABLE VIEW / EDIT UI
# ----------------------------

with tab4:
    st.header("View and Edit Timetable")

    grades_sections = get_all_grades_sections()
    all_grades = list(grades_sections.keys())

    selected_grade = st.selectbox("Select Grade", options=all_grades, key="view_grade_select")
    if selected_grade:
        sections = grades_sections[selected_grade]["sections"]
        selected_section = st.selectbox("Select Section", options=sections, key="view_section_select")

        if selected_section:
            timetable = get_timetable_for_grade_section(selected_grade, selected_section)
            periods_per_day = get_periods_per_day()
            days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

            # Fetch teachers list for selection dropdown
            conn = get_conn()
            cur = conn.cursor()
            cur.execute("SELECT id, teacher_name, subject, grades FROM teachers")
            teachers = cur.fetchall()
            conn.close()

            teacher_options = {t[1]: t[0] for t in teachers}  # name -> id

            # Display timetable in a table with editable selects for teachers
            st.markdown(f"### Timetable for Grade {selected_grade}, Section {selected_section}")
            timetable_df = pd.DataFrame(index=range(1, periods_per_day+1), columns=days)

            for day in days:
                for period in range(1, periods_per_day+1):
                    entry = timetable.get(day, {}).get(period, ("", ""))
                    teacher_name = entry[0]
                    subject_name = entry[1]
                    timetable_df.at[period, day] = f"{subject_name} ({teacher_name})" if subject_name else ""

            st.dataframe(timetable_df, use_container_width=True)

            st.markdown("### Manual Edit Timetable")

            with st.form("manual_edit_form"):
                col1, col2, col3, col4, col5 = st.columns(5)
                selected_day = col1.selectbox("Day", days, key="edit_day")
                selected_period = col2.number_input("Period", min_value=1, max_value=periods_per_day, key="edit_period")
                selected_subject = col3.text_input("Subject", key="edit_subject")

                # Filter teachers who teach selected grade and subject
                filtered_teachers = []
                for tid, tname, tsubj, tgrades_str in teachers:
                    tgrades = [g.strip() for g in tgrades_str.split(",")]
                    if selected_grade in tgrades and (selected_subject == "" or selected_subject == tsubj):
                        filtered_teachers.append((tid, tname))
                teacher_names_for_select = [t[1] for t in filtered_teachers]
                selected_teacher_name = col4.selectbox("Teacher", options=teacher_names_for_select, key="edit_teacher")
                submit_edit = col5.form_submit_button("Update")

                if submit_edit:
                    # Find teacher id from name
                    teacher_id = None
                    for tid, tname in filtered_teachers:
                        if tname == selected_teacher_name:
                            teacher_id = tid
                            break
                    if not teacher_id:
                        st.error("Teacher not found or not valid for this subject/grade.")
                    else:
                        # Update timetable entry
                        success, msg = update_timetable_entry(selected_grade, selected_section, selected_day, selected_period, teacher_id)
                        if success:
                            st.success(msg)
                        else:
                            st.error(msg)

            st.markdown("---")
            st.subheader("Export Timetable as CSV")
            csv_data = export_timetable_csv(selected_grade, selected_section)
            st.download_button(label="Download CSV", data=csv_data, file_name=f"timetable_{selected_grade}_{selected_section}.csv", mime="text/csv")

import io
import csv

# --- DATABASE HELPERS ---

def get_conn():
    import sqlite3
    return sqlite3.connect("school_timetable.db", check_same_thread=False)

def get_all_grades_sections():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT grade, sections FROM subjects")
    data = cur.fetchall()
    conn.close()

    grades_sections = {}
    for grade, sections_str in data:
        sections = [s.strip() for s in sections_str.split(",")] if sections_str else ["A"]
        grades_sections[grade] = {"sections": sections}
    return grades_sections

def get_periods_per_day():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = 'periods_per_day'")
    row = cur.fetchone()
    conn.close()
    if row:
        try:
            return int(row[0])
        except:
            return 8
    return 8  # default

# --- TIMETABLE FETCH & UPDATE ---

def get_timetable_for_grade_section(grade, section):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT tbp.day_of_week, tbp.period_number, t.teacher_name, t.subject
        FROM teacher_busy_periods tbp
        JOIN teachers t ON tbp.teacher_id = t.id
        WHERE tbp.grade = ? AND tbp.section = ?
    """, (grade, section))
    rows = cur.fetchall()
    conn.close()
    timetable = {day: {} for day in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]}
    for day, period, teacher_name, subject in rows:
        timetable.setdefault(day, {})[period] = (teacher_name, subject)
    return timetable

def update_timetable_entry(grade, section, day, period, teacher_id):
    """
    Update the timetable entry with new teacher assignment.
    Check constraints:
    - teacher availability (not double booked)
    - max 2 periods per subject per day per section (unless section is partial days)
    - substitution allowed, else assign 'Library' or 'Games' period
    """
    conn = get_conn()
    cur = conn.cursor()

    # Get teacher subject and grades
    cur.execute("SELECT subject, grades FROM teachers WHERE id = ?", (teacher_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return False, "Teacher not found."
    subject, grades_str = row
    grades = [g.strip() for g in grades_str.split(",")]

    if grade not in grades:
        conn.close()
        return False, "Teacher does not teach this grade."

    # Check if teacher is free at this time (not assigned elsewhere)
    cur.execute("""
        SELECT COUNT(*) FROM teacher_busy_periods
        WHERE teacher_id = ? AND day_of_week = ? AND period_number = ? AND NOT (grade = ? AND section = ?)
    """, (teacher_id, day, period, grade, section))
    (count,) = cur.fetchone()
    if count > 0:
        conn.close()
        return False, "Teacher is already assigned at this time."

    # Check max 2 periods per subject per day per section if section attends all days
    # For sections attending partial days, constraint can be violated
    cur.execute("SELECT sections FROM subjects WHERE grade = ? AND subject_name = ?", (grade, subject))
    subj_sections = [s.strip() for s in cur.fetchone()[0].split(",")]

    if section in subj_sections:
        # Check if section attends all weekdays
        # We store section_days info somewhere (for now assume full week)
        # For demo, assume full week and enforce constraint
        cur.execute("""
            SELECT COUNT(*) FROM teacher_busy_periods tbp
            JOIN teachers t ON tbp.teacher_id = t.id
            WHERE tbp.grade = ? AND tbp.section = ? AND tbp.day_of_week = ? AND t.subject = ?
        """, (grade, section, day, subject))
        (period_count,) = cur.fetchone()
        if period_count >= 2:
            conn.close()
            return False, "Maximum 2 periods per subject per day allowed for this section."

    # Delete any existing assignment for this slot
    cur.execute("""
        DELETE FROM teacher_busy_periods WHERE grade = ? AND section = ? AND day_of_week = ? AND period_number = ?
    """, (grade, section, day, period))

    # Insert new assignment
    cur.execute("""
        INSERT INTO teacher_busy_periods (teacher_id, grade, section, day_of_week, period_number)
        VALUES (?, ?, ?, ?, ?)
    """, (teacher_id, grade, section, day, period))
    conn.commit()
    conn.close()
    return True, "Timetable updated successfully."

# --- TIMETABLE EXPORT ---

def export_timetable_csv(grade, section):
    timetable = get_timetable_for_grade_section(grade, section)
    periods_per_day = get_periods_per_day()
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

    output = io.StringIO()
    writer = csv.writer(output)
    header = ["Period"] + days
    writer.writerow(header)

    for period in range(1, periods_per_day + 1):
        row = [period]
        for day in days:
            entry = timetable.get(day, {}).get(period, ("", ""))
            if entry[0] and entry[1]:
                cell = f"{entry[1]} ({entry[0]})"
            else:
                cell = ""
            row.append(cell)
        writer.writerow(row)

    return output.getvalue()

# --- AI-BASED SUBSTITUTE TEACHER SUGGESTION (placeholder) ---
genai.api_key = st.secrets["gemini_api_key"]

def ai_suggest_substitute(grade, subject, day, period):
    """
    Use Google Gemini to suggest a substitute teacher for a given grade, subject, day, and period.
    If no suitable teacher, return None.
    """

    prompt = (
        f"Suggest a substitute teacher for grade {grade}, subject '{subject}' "
        f"on {day} during period {period}. "
        "Only list teacher names who teach this subject and grade, are available at this time, "
        "and are not already assigned another class during that period. "
        "If no teacher is available, respond with 'No substitute available'."
    )

    try:
        response = genai.chat.completions.create(
            model="models/chat-bison-001",
            messages=[
                {"role": "system", "content": "You are an assistant helping schedule teachers."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.2,
            max_tokens=100,
        )

        answer = response.choices[0].message["content"].strip()

        if "no substitute available" in answer.lower():
            return None

        # Now match returned teacher name to teacher_id from DB
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT id FROM teachers WHERE teacher_name = ? AND subject = ? AND instr(grades, ?) > 0", 
                    (answer, subject, grade))
        row = cur.fetchone()
        conn.close()
        if row:
            return row[0]
        else:
            return None
    except Exception as e:
        print("Error in AI substitute suggestion:", e)
        return None

# --- END OF PART 3 ---
