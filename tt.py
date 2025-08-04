import streamlit as st
import sqlite3
import pandas as pd
import random
import os
import io
import google.generativeai as genai

# ---------------------- CONFIG ----------------------
DB_PATH = "timetable.db"
genai.configure(api_key=st.secrets.get("GEMINI_API_KEY", ""))

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

# -------------------- DATABASE ----------------------

def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    # Teachers
    cur.execute("""
    CREATE TABLE IF NOT EXISTS teachers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        teacher_name TEXT NOT NULL,
        subject TEXT NOT NULL,
        grades TEXT NOT NULL
    )""")
    # Subjects
    cur.execute("""
    CREATE TABLE IF NOT EXISTS subjects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subject_name TEXT NOT NULL,
        grade TEXT NOT NULL,
        periods_per_week INTEGER NOT NULL,
        sections TEXT NOT NULL DEFAULT 'A', -- comma-separated sections
        active_days TEXT NOT NULL DEFAULT 'Monday,Tuesday,Wednesday,Thursday,Friday' -- comma-separated days
    )""")
    # Subject colors
    cur.execute("""
    CREATE TABLE IF NOT EXISTS subject_colors (
        subject_name TEXT PRIMARY KEY,
        color_code TEXT
    )""")
    # Busy periods (timetable slots)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS teacher_busy_periods (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        teacher_id INTEGER NOT NULL,
        grade TEXT NOT NULL,
        section TEXT NOT NULL,
        period_number INTEGER NOT NULL,
        day_of_week TEXT NOT NULL,
        FOREIGN KEY(teacher_id) REFERENCES teachers(id)
    )""")
    # Settings (key-value)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )""")
    conn.commit()
    conn.close()

# ----------------- UTILITIES ------------------------

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
    cur.execute("INSERT OR IGNORE INTO subject_colors(subject_name, color_code) VALUES (?, ?)", (subject_name, color))
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

# ------------------ DATA ACCESS ----------------------

def get_teachers():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, teacher_name, subject, grades FROM teachers")
    data = cur.fetchall()
    conn.close()
    return data

def get_teachers_for_grade_section(grade, section):
    # Return teachers who teach the grade & section (section is in grades CSV)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, teacher_name, subject, grades FROM teachers")
    teachers = []
    for tid, tname, sub, grades_csv in cur.fetchall():
        grades = [g.strip() for g in grades_csv.split(",")]
        if grade in grades:
            teachers.append((tid, tname, sub))
    conn.close()
    return teachers

def get_subjects():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, subject_name, grade, periods_per_week, sections, active_days FROM subjects")
    data = cur.fetchall()
    conn.close()
    return data

def get_subjects_for_grade(grade):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT subject_name, periods_per_week, sections, active_days FROM subjects WHERE grade=?", (grade,))
    subs = cur.fetchall()
    conn.close()
    return subs

def clear_timetable():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM teacher_busy_periods")
    conn.commit()
    conn.close()

def clear_timetable_for_grade_section(grade, section):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM teacher_busy_periods WHERE grade=? AND section=?", (grade, section))
    conn.commit()
    conn.close()

def get_busy_periods():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT tbp.id, t.teacher_name, t.subject, tbp.grade, tbp.section, tbp.period_number, tbp.day_of_week
        FROM teacher_busy_periods tbp
        JOIN teachers t ON tbp.teacher_id = t.id
        ORDER BY tbp.day_of_week, tbp.period_number, tbp.grade, tbp.section
    """)
    data = cur.fetchall()
    conn.close()
    return data

# ---------------- TIMETABLE GENERATION -----------------

def generate_timetable(ai_mode: bool, absent_teachers, max_periods_per_subject_per_day=2):
    """
    Generates timetable for all grades & sections.
    Enforces:
    - No teacher overlaps
    - Max periods per subject per day per section (except sections with fewer active days)
    - Substitutions for absent teachers (fallback to 'Games' if no sub)
    - Each grade & teacher gets >=1 Games period/week
    """

    conn = get_conn()
    cur = conn.cursor()

    # Load all subjects grouped by grade & section
    cur.execute("SELECT DISTINCT grade FROM subjects")
    grades = [r[0] for r in cur.fetchall()]

    # Clear all timetable entries before generation
    clear_timetable()

    # Build teacher availability
    teachers = get_teachers()
    teacher_dict = {tid: {"name": tname, "subject": sub, "grades": grades_str.split(",")} for tid, tname, sub, grades_str in teachers}

    # For easy teacher lookup by grade and subject
    grade_subject_teachers = {}
    for tid, tname, sub, grades_csv in teachers:
        for g in grades_csv.split(","):
            key = (g.strip(), sub)
            grade_subject_teachers.setdefault(key, []).append(tid)

    # Get all subjects
    subjects = get_subjects()

    # Data structure for timetable: {grade: {section: {day: {period: (teacher_id, subject)}}}}
    timetable = {}
    # Track teacher load per day and global assignments
    teacher_load = {}
    teacher_games_assigned = set()
    grade_games_assigned = {}

    # Helper to assign games period if needed
    def assign_games_period(grade, section, day, period):
        # find a 'Games' teacher who can teach this grade
        possible_games_teachers = grade_subject_teachers.get((grade, "Games"), [])
        if not possible_games_teachers:
            return False
        for t_id in possible_games_teachers:
            # Check if teacher free this period
            if teacher_load.get((t_id, day), 0) < 5:
                # Assign Games period
                timetable.setdefault(grade, {}).setdefault(section, {}).setdefault(day, {})[period] = (t_id, "Games")
                teacher_load[(t_id, day)] = teacher_load.get((t_id, day), 0) + 1
                teacher_games_assigned.add((t_id, day))
                grade_games_assigned.setdefault(grade, set()).add(day)
                return True
        return False

    # Fill timetable for each grade & section
    for grade in grades:
        # Get subjects for grade
        grade_subjects = [s for s in subjects if s[2] == grade]
        # Parse all sections in grade subjects
        sections = set()
        for sub_name, _, secs_csv, active_days_csv in grade_subjects:
            for sec in secs_csv.split(","):
                sections.add(sec.strip())
        if not sections:
            sections = {"A"}
        timetable.setdefault(grade, {})

        for section in sections:
            timetable[grade].setdefault(section, {})
            # Get active days for this section (intersection of all subjects active days)
            section_active_days = None
            for sub_name, _, secs_csv, active_days_csv in grade_subjects:
                if section in [s.strip() for s in secs_csv.split(",")]:
                    days = set([d.strip() for d in active_days_csv.split(",")])
                    if section_active_days is None:
                        section_active_days = days
                    else:
                        section_active_days = section_active_days.intersection(days)
            if section_active_days is None:
                section_active_days = set(WEEKDAYS)
            # Convert back to list sorted by weekday order
            section_active_days = [d for d in WEEKDAYS if d in section_active_days]

            # For each active day, initialize timetable grid (periods 1-8)
            for day in WEEKDAYS:
                timetable[grade][section][day] = {}

            # Assign subjects
            # Build subject periods counts per section
            subject_periods = {}
            for sub_name, periods_per_week, secs_csv, active_days_csv in grade_subjects:
                if section in [s.strip() for s in secs_csv.split(",")]:
                    # Scale periods per week based on active days ratio
                    total_days = len([d for d in active_days_csv.split(",") if d.strip() in WEEKDAYS])
                    active_days_count = len(section_active_days)
                    adjusted_periods = max(1, round(periods_per_week * active_days_count / total_days))
                    subject_periods[sub_name] = adjusted_periods

            # Periods per day fixed at 8
            periods_per_day = 8

            # Track how many periods per subject assigned per day to this section
            subject_day_count = {day: {} for day in WEEKDAYS}

            # Teacher daily load tracking for overlaps
            # key = (teacher_id, day), val = count of periods assigned
            # Max 5 periods per day
            teacher_daily_load = {}

            # Assign subjects randomly but enforce constraints
            # Flatten all periods to assign: total_periods = sum of all subject_periods
            all_subject_slots = []
            for sub, cnt in subject_periods.items():
                all_subject_slots.extend([sub]*cnt)
            random.shuffle(all_subject_slots)

            for sub in all_subject_slots:
                placed = False
                attempts = 0
                while not placed and attempts < 100:
                    attempts += 1
                    day = random.choice(section_active_days)
                    period = random.randint(1, periods_per_day)
                    if period in timetable[grade][section][day]:
                        continue  # already assigned
                    # Check subject daily limit if section attends all days
                    max_per_day = max_periods_per_subject_per_day
                    if len(section_active_days) < 5:
                        max_per_day = 10  # relax limit for sections attending fewer days
                    if subject_day_count[day].get(sub, 0) >= max_per_day:
                        continue
                    # Find available teachers for this grade and subject excluding absentees
                    candidates = grade_subject_teachers.get((grade, sub), [])
                    candidates = [tid for tid in candidates if
                                  tid not in absent_teachers.get(day, [])]
                    # Also exclude teachers who have overlap at this day/period
                    available_teachers = []
                    for tid in candidates:
                        if teacher_daily_load.get((tid, day), 0) >= 5:
                            continue
                        # Check if teacher is already assigned this period (overlap)
                        conflict = False
                        for g in timetable:
                            for sec in timetable[g]:
                                if day in timetable[g][sec] and period in timetable[g][sec][day]:
                                    if timetable[g][sec][day][period][0] == tid:
                                        conflict = True
                                        break
                            if conflict:
                                break
                        if not conflict:
                            available_teachers.append(tid)
                    if not available_teachers:
                        continue
                    chosen_teacher = random.choice(available_teachers)
                    # Assign
                    timetable[grade][section][day][period] = (chosen_teacher, sub)
                    subject_day_count[day][sub] = subject_day_count[day].get(sub, 0) + 1
                    teacher_daily_load[(chosen_teacher, day)] = teacher_daily_load.get((chosen_teacher, day), 0) + 1
                    placed = True

                if not placed:
                    # Could not place this subject period, fallback to games later
                    pass

            # Ensure at least one Games period for each teacher & grade/week
            # Assign to free slots and free teachers
            for day in section_active_days:
                for period in range(1, periods_per_day+1):
                    if period not in timetable[grade][section][day]:
                        # Try to assign games teacher for this grade
                        assigned_games = False
                        possible_games_teachers = grade_subject_teachers.get((grade, "Games"), [])
                        random.shuffle(possible_games_teachers)
                        for t_id in possible_games_teachers:
                            if teacher_daily_load.get((t_id, day), 0) < 5:
                                conflict = False
                                for g in timetable:
                                    for sec in timetable[g]:
                                        if day in timetable[g][sec] and period in timetable[g][sec][day]:
                                            if timetable[g][sec][day][period][0] == t_id:
                                                conflict = True
                                                break
                                    if conflict:
                                        break
                                if not conflict:
                                    timetable[grade][section][day][period] = (t_id, "Games")
                                    teacher_daily_load[(t_id, day)] = teacher_daily_load.get((t_id, day), 0) + 1
                                    teacher_games_assigned.add((t_id, day))
                                    grade_games_assigned.setdefault(grade, set()).add(day)
                                    assigned_games = True
                                    break
                        if not assigned_games:
                            # Last resort assign dummy games teacher (id -1)
                            timetable[grade][section][day][period] = (-1, "Games")

    # Save timetable to DB
    for grade in timetable:
        for section in timetable[grade]:
            for day in timetable[grade][section]:
                for period in timetable[grade][section][day]:
                    t_id, sub = timetable[grade][section][day][period]
                    cur.execute("""
                    INSERT INTO teacher_busy_periods (teacher_id, grade, section, period_number, day_of_week)
                    VALUES (?, ?, ?, ?, ?)
                    """, (t_id, grade, section, period, day))
    conn.commit()
    conn.close()

    return timetable

# ---------------- AI GENERATION -----------------------

def ai_generate_timetable_prompt(grade, subjects, sections, absent_teachers):
    prompt = f"Create a weekly timetable for grade {grade} with sections {', '.join(sections)}.\n"
    prompt += "Subjects and periods per week:\n"
    for sub, periods, secs, days in subjects:
        prompt += f"- {sub}: {periods} periods/week, sections: {secs}, active days: {days}\n"
    prompt += f"Absent teachers per day: {absent_teachers}\n"
    prompt += "Constraints:\n"
    prompt += "- No teacher teaches two classes at the same time.\n"
    prompt += "- Maximum two periods per subject per day per section (except if section attends less than 5 days).\n"
    prompt += "- Substitute absent teachers if possible, else assign 'Games'.\n"
    prompt += "- Each grade and each teacher should have at least one Games period per week.\n"
    prompt += "Output the timetable as a JSON object keyed by section, day, period with teacher and subject.\n"
    return prompt

def generate_timetable_ai(grade, absent_teachers, subjects, sections):
    prompt = ai_generate_timetable_prompt(grade, subjects, sections, absent_teachers)
    try:
        response = genai.chat.create(
            model="models/chat-bison-001",
            messages=[
                {"author": "user", "content": prompt}
            ]
        )
        text = response.text
        # Try to parse JSON from response
        import json
        timetable_json = json.loads(text)
        return timetable_json
    except Exception as e:
        st.error(f"AI generation failed: {e}")
        return None

# ------------------ STREAMLIT APP ---------------------

def main():
    st.set_page_config(page_title="School Timetable Generator", layout="wide")
    init_db()

    # Dark/light mode toggle
    theme = st.sidebar.selectbox("Theme", ["Light", "Dark"], index=1)
    if theme == "Dark":
        st.markdown(
            """
            <style>
            .reportview-container {
                background-color: #222222;
                color: #eee;
            }
            </style>
            """, unsafe_allow_html=True
        )
    else:
        st.markdown(
            """
            <style>
            .reportview-container {
                background-color: #ffffff;
                color: #000000;
            }
            </style>
            """, unsafe_allow_html=True
        )

    tabs = st.tabs(["Setup", "Absentees", "Generate Timetable", "View / Edit Timetable"])

    # ----- Setup tab -----
    with tabs[0]:
        st.header("Teachers Management")
        teacher_file = st.file_uploader("Upload Teachers CSV (teacher_name,subject,grades)", type=["csv"], key="teacher_csv")
        if teacher_file:
            df = pd.read_csv(teacher_file)
            conn = get_conn()
            cur = conn.cursor()
            for _, row in df.iterrows():
                cur.execute("INSERT INTO teachers (teacher_name, subject, grades) VALUES (?, ?, ?)",
                            (row["teacher_name"], row["subject"], row["grades"]))
            conn.commit()
            conn.close()
            st.success("Teachers uploaded from CSV!")

        st.markdown("**Add Teacher Manually**")
        with st.form("manual_teacher_form"):
            t_name = st.text_input("Teacher Name")
            t_subject = st.text_input("Subject")
            t_grades = st.text_input("Grades (comma separated, e.g. 10,11)")
            submitted = st.form_submit_button("Add Teacher")
            if submitted:
                if not (t_name and t_subject and t_grades):
                    st.warning("Fill all fields")
                else:
                    conn = get_conn()
                    cur = conn.cursor()
                    cur.execute("INSERT INTO teachers (teacher_name, subject, grades) VALUES (?, ?, ?)",
                                (t_name.strip(), t_subject.strip(), t_grades.strip()))
                    conn.commit()
                    conn.close()
                    st.success(f"Teacher {t_name} added!")

        st.header("Subjects Management")
        subject_file = st.file_uploader("Upload Subjects CSV (subject_name,grade,periods_per_week,sections,active_days)", type=["csv"], key="subject_csv")
        if subject_file:
            df = pd.read_csv(subject_file)
            conn = get_conn()
            cur = conn.cursor()
            for _, row in df.iterrows():
                cur.execute("""INSERT INTO subjects (subject_name, grade, periods_per_week, sections, active_days)
                            VALUES (?, ?, ?, ?, ?)""",
                            (row["subject_name"], row["grade"], int(row["periods_per_week"]), row.get("sections", "A"), row.get("active_days", "Monday,Tuesday,Wednesday,Thursday,Friday")))
            conn.commit()
            conn.close()
            st.success("Subjects uploaded from CSV!")

        st.markdown("**Add Subject Manually**")
        with st.form("manual_subject_form"):
            s_name = st.text_input("Subject Name")
            s_grade = st.text_input("Grade")
            s_periods = st.number_input("Periods Per Week", min_value=1, max_value=50, step=1)
            s_sections = st.text_input("Sections (comma separated, default A)", value="A")
            s_active_days = st.multiselect("Active Days for Section", WEEKDAYS, default=WEEKDAYS)
            s_active_days_str = ",".join(s_active_days)
            submitted_subj = st.form_submit_button("Add Subject")
            if submitted_subj:
                if not (s_name and s_grade and s_periods):
                    st.warning("Fill all mandatory fields")
                else:
                    conn = get_conn()
                    cur = conn.cursor()
                    cur.execute("""INSERT INTO subjects (subject_name, grade, periods_per_week, sections, active_days)
                                VALUES (?, ?, ?, ?, ?)""",
                                (s_name.strip(), s_grade.strip(), s_periods, s_sections.strip(), s_active_days_str))
                    conn.commit()
                    conn.close()
                    st.success(f"Subject {s_name} added!")

        # Number of periods in a day
        st.header("Settings")
        periods_per_day = st.number_input("Number of periods per day", min_value=4, max_value=12, value=8)

        # Save settings
        if st.button("Save Settings"):
            conn = get_conn()
            cur = conn.cursor()
            cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", ("periods_per_day", str(periods_per_day)))
            conn.commit()
            conn.close()
            st.success("Settings saved!")

    # ----- Absentees tab -----
    with tabs[1]:
        st.header("Mark Absent Teachers Per Day")
        absent_teachers = {}
        conn = get_conn()
        cur = conn.cursor()
        teachers = get_teachers()
        teachers_dict = {tid: tname for tid, tname, _, _ in teachers}
        for day in WEEKDAYS:
            with st.expander(f"{day}"):
                selected = st.multiselect(f"Select absent teachers on {day}", [tname for _, tname in teachers], key=f"absent_{day}")
                absent_ids = [tid for tid, tname, _, _ in teachers if tname in selected]
                absent_teachers[day] = absent_ids

    # ----- Generate Timetable tab -----
    with tabs[2]:
        st.header("Generate Timetable")

        # Load settings
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE key='periods_per_day'")
        res = cur.fetchone()
        if res:
            periods_per_day = int(res[0])
        else:
            periods_per_day = 8

        st.write(f"Periods per day (from settings): {periods_per_day}")

        st.write("Select AI or Manual generation:")
        generation_mode = st.radio("Generation mode", ["AI-powered", "Manual randomized"], index=0)

        if st.button("Generate Timetable for All Grades"):
            # For demo, absent teachers from tab 2 used, pass as dictionary
            at = absent_teachers
            if generation_mode == "Manual randomized":
                generate_timetable(ai_mode=False, absent_teachers=at, max_periods_per_subject_per_day=2)
                st.success("Timetable generated manually.")
            else:
                # Run AI generation per grade - demo simplified
                conn = get_conn()
                cur = conn.cursor()
                cur.execute("SELECT DISTINCT grade FROM subjects")
                grades = [r[0] for r in cur.fetchall()]
                for grade in grades:
                    subs = get_subjects_for_grade(grade)
                    # Identify sections:
                    secs_set = set()
                    for _, _, secs_csv, _ in subs:
                        for sec in secs_csv.split(","):
                            secs_set.add(sec.strip())
                    timetable_json = generate_timetable_ai(grade, at, subs, list(secs_set))
                    if timetable_json:
                        st.write(f"AI timetable for grade {grade} (preview):")
                        st.json(timetable_json)
                st.info("AI generation attempted for all grades (check output above).")

    # ----- View/Edit Timetable tab -----
    with tabs[3]:
        st.header("View and Manual Edit Timetable")

        conn = get_conn()
        cur = conn.cursor()
        # Get all grades & sections
        cur.execute("SELECT DISTINCT grade FROM subjects")
        grades = [r[0] for r in cur.fetchall()]
        selected_grade = st.selectbox("Select Grade", grades, key="view_grade")
        # Find sections for selected grade
        cur.execute("SELECT DISTINCT sections FROM subjects WHERE grade=?", (selected_grade,))
        sections_csv_list = cur.fetchall()
        sections = set()
        for (csv_str,) in sections_csv_list:
            for sec in csv_str.split(","):
                sections.add(sec.strip())
        if not sections:
            sections = {"A"}
        selected_section = st.selectbox("Select Section", sorted(list(sections)), key="view_section")

        # Load timetable for grade and section
        cur.execute("""
            SELECT tbp.id, t.teacher_name, t.subject, tbp.period_number, tbp.day_of_week
            FROM teacher_busy_periods tbp
            LEFT JOIN teachers t ON tbp.teacher_id = t.id
            WHERE tbp.grade=? AND tbp.section=?
            ORDER BY tbp.day_of_week, tbp.period_number
        """, (selected_grade, selected_section))
        rows = cur.fetchall()

        # Load subjects for grade & section
        cur.execute("SELECT subject_name FROM subjects WHERE grade=?", (selected_grade,))
        all_subs = [r[0] for r in cur.fetchall()]
        # Load teachers for grade
        cur.execute("SELECT id, teacher_name, subject FROM teachers")
        all_teachers = cur.fetchall()

        # Build timetable dict day->period->(teacher,subject)
        timetable_data = {day: {p: ("", "") for p in range(1, periods_per_day+1)} for day in WEEKDAYS}
        id_map = {}
        for id_, tname, sub, period, day in rows:
            timetable_data[day][period] = (tname if tname else "Games", sub)
            id_map[(day, period)] = id_

        st.markdown("### Timetable")
        for day in WEEKDAYS:
            st.markdown(f"**{day}**")
            cols = st.columns(periods_per_day)
            for period in range(1, periods_per_day+1):
                key_base = f"{selected_grade}_{selected_section}_{day}_{period}"
                current_teacher, current_subject = timetable_data[day][period]
                # Select subject
                subj_sel = st.selectbox(f"Subject (Period {period})", options=[""] + all_subs, index=0 if current_subject=="" else ([""] + all_subs).index(current_subject), key=f"subj_{key_base}", label_visibility="collapsed")
                # Select teacher (filtered by subject)
                filtered_teachers = [t for t in all_teachers if t[2] == subj_sel]
                teacher_names = [t[1] for t in filtered_teachers]
                if current_teacher not in teacher_names:
                    teacher_names.insert(0, current_teacher)
                teacher_sel = st.selectbox(f"Teacher (Period {period})", options=[""] + teacher_names, index=0 if current_teacher=="" else ([""] + teacher_names).index(current_teacher), key=f"teach_{key_base}", label_visibility="collapsed")
                # On change, update DB if valid
                if st.button(f"Save Period {period} {day}", key=f"save_{key_base}"):
                    if not subj_sel or not teacher_sel:
                        st.warning("Subject and teacher must be selected")
                    else:
                        # Validate no teacher overlap for that day/period
                        conflict = False
                        for g in grades:
                            cur.execute("""
                            SELECT tbp.id, t.teacher_name, t.subject, tbp.grade, tbp.section, tbp.period_number, tbp.day_of_week
                            FROM teacher_busy_periods tbp
                            JOIN teachers t ON tbp.teacher_id = t.id
                            WHERE tbp.day_of_week=? AND tbp.period_number=? AND t.teacher_name=? AND NOT (tbp.grade=? AND tbp.section=?)
                            """, (day, period, teacher_sel, selected_grade, selected_section))
                            if cur.fetchall():
                                conflict = True
                                break
                        if conflict:
                            st.error(f"Teacher {teacher_sel} already assigned at {day} period {period} in another grade/section.")
                        else:
                            # Insert or update record
                            teacher_id = None
                            for t in all_teachers:
                                if t[1] == teacher_sel:
                                    teacher_id = t[0]
                                    break
                            if not teacher_id:
                                st.error("Teacher not found in DB")
                            else:
                                if (day, period) in id_map:
                                    # update
                                    cur.execute("""
                                        UPDATE teacher_busy_periods SET teacher_id=? WHERE id=?
                                    """, (teacher_id, id_map[(day, period)]))
                                else:
                                    cur.execute("""
                                        INSERT INTO teacher_busy_periods (teacher_id, grade, section, period_number, day_of_week)
                                        VALUES (?, ?, ?, ?, ?)
                                    """, (teacher_id, selected_grade, selected_section, period, day))
                                conn.commit()
                                st.success(f"Period {period} on {day} updated.")

    # Download CSVs
    st.sidebar.header("Download Data CSVs")
    if st.sidebar.button("Download Teachers CSV"):
        conn = get_conn()
        df = pd.read_sql_query("SELECT teacher_name, subject, grades FROM teachers", conn)
        conn.close()
        csv = df.to_csv(index=False)
        st.sidebar.download_button("Download teachers.csv", data=csv, file_name="teachers.csv", mime="text/csv")
    if st.sidebar.button("Download Subjects CSV"):
        conn = get_conn()
        df = pd.read_sql_query("SELECT subject_name, grade, periods_per_week, sections, active_days FROM subjects", conn)
        conn.close()
        csv = df.to_csv(index=False)
        st.sidebar.download_button("Download subjects.csv", data=csv, file_name="subjects.csv", mime="text/csv")

if __name__ == "__main__":
    main()
