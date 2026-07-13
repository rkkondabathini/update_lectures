where# LectureUpdate — Automation Scripts

Playwright-based bulk automation for the Masai admin platforms — lecture creation/updates/deletion, section updates, and cohort settings.

---

## Setup (run once per terminal session)

```bash
cd /Users/inno/Projects/lectureUpdate
source .venv/bin/activate
```

First-time install:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

---

## Quick Reference — All Commands

Activate the venv first (every new terminal):

```bash
source /Users/inno/Projects/lectureUpdate/.venv/bin/activate
```

Then run any of:


| #   | Tool                  | Command                                                                                             | What it does                                                                          |
| --- | --------------------- | --------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| 1   | createLecture         | `cd /Users/inno/Projects/lectureUpdate/createLecture && python create_lecture.py`                   | Bulk create lectures                                                                  |
| 2   | updateLecture         | `cd /Users/inno/Projects/lectureUpdate/updateLecture && python update_lecture.py`                   | Bulk update lecture fields (category, module, tags, etc.)                             |
| 3   | updateTitles          | `cd /Users/inno/Projects/lectureUpdate/updateTitles && python update_title.py`                      | Bulk update lecture titles                                                            |
| 4   | deleteLecture         | `cd /Users/inno/Projects/lectureUpdate/deleteLecture && python delete_lecture.py`                   | Bulk delete lectures (requires `DELETE <count>` confirmation)                         |
| 5   | updateSection         | `cd /Users/inno/Projects/lectureUpdate/updateSection && python update_section.py`                   | Bulk update section settings (display name, type, module, Enable Zoom Web View, etc.) |
| 6   | addMenu               | `cd /Users/inno/Projects/lectureUpdate/addMenu && python add_menu.py`                               | Bulk create menu items (categories + values)                                          |
| 7   | updateStudentCode     | `cd /Users/inno/Projects/lectureUpdate/updateStudentCode && python update_student_code.py`          | Bulk update LMS UserName / student code                                               |
| 7b  | sendMessages          | `cd /Users/inno/Projects/lectureUpdate/sendMessages && python send_messages.py`                     | Bulk send LMS messages (uses separate Student Experience account)                     |
| 8   | updateMasaiCohorts    | `cd /Users/inno/Projects/lectureUpdate/updateMasaiCohorts && python update_cohort.py`               | Bulk update Masai cohort settings (add `--start-cohort N` to resume)                  |
| 9   | updatePrepleafCohorts | `cd /Users/inno/Projects/lectureUpdate/updatePrepleafCohorts && python update_cohort.py`            | Bulk update Prepleaf/iHub cohort settings (add `--start-cohort N` to resume)          |
| 10  | deleteUserFromSection | `cd /Users/inno/Projects/lectureUpdate/deleteUserFromSection && python delete_user_from_section.py` | Bulk remove users from sections (requires `DELETE <count>` confirmation)              |


For each tool: drop the CSV in `<tool>/input/` before running. Results land in `<tool>/logs/`.

---

## Directory Structure

```
lectureUpdate/
│
├── createLecture/         Bulk lecture creator
├── updateLecture/         Bulk lecture field updater (category, module, tags…)
├── updateTitles/          Bulk lecture title updater
├── deleteLecture/         Bulk lecture deleter (with confirmation)
│
├── updateSection/         Bulk section settings updater
├── addMenu/               Bulk menu-item creator (tickets categories etc.)
├── updateStudentCode/     Bulk student-code (UserName) updater
├── sendMessages/          Bulk LMS message sender (separate Student Experience login)
│
├── updateMasaiCohorts/    Masai cohort settings updater
├── updatePrepleafCohorts/ Prepleaf (iHub) cohort settings updater
└── deleteUserFromSection/ Bulk remove users from sections
```

Each tool folder contains:

- `<script>.py` — the runnable script
- `input/` — drop your input CSV(s) here before running
- `logs/` — per-run `.log` and result `.csv`
- `logs/archive/` — copy of the input CSV kept per run
- `browser_profile/` (cohort tools only) — created on first OTP login

---

## 1. createLecture — Bulk lecture creator

Creates new lectures on `experience-admin.masaischool.com/lectures/create/`.

**Two CSVs required in `createLecture/input/`:**

1. **Data CSV** with columns:
  `title, batch, section, category, tags, type, schedule_date, schedule_time, concludes_date, concludes_time, host_email, zoom_link, module, show_feedback`
   plus EITHER `mandatory` (TRUE/FALSE) OR `optional` (yes/no — inverse meaning).
2. **Hosts CSV** with columns: `Name, Email` — used to translate `host_email` → display name for the Primary host search.

**Run:**

```bash
cd createLecture
python create_lecture.py
```

Field-fill order: Title → Type → Category → Module → Tags → Primary Host → Batch → Section → Test Groups → Zoom → Mandatory → ShowFB → Schedule → Concludes → Create.

The script verifies Schedule/Concludes right before submit and re-applies if they got reset by other field interactions.

---

## 2. updateLecture — Bulk lecture field updater

Updates category, module, tags, mandatory flag, and show-feedback toggle.

**Required CSV columns:**


| Column                  | Description                       |
| ----------------------- | --------------------------------- |
| `lecture_url`           | Full URL of the lecture edit page |
| `updated_category`      | New category                      |
| `updated_module`        | New module                        |
| `updated_tags`          | Comma-separated tags              |
| `updated_mandatory`     | `TRUE` / `FALSE`                  |
| `updated_show_feedback` | `TRUE` / `FALSE`                  |


**Run:**

```bash
cd updateLecture
python update_lecture.py
```

For each row: reads current DOM values, skips fields already correct, updates the rest, verifies (retries once on mismatch), then saves.

---

## 3. updateTitles — Bulk title updater

**Required CSV columns:**


| Column          | Description                       |
| --------------- | --------------------------------- |
| `lecture_url`   | Full URL of the lecture edit page |
| `updated_title` | New title text                    |


**Run:**

```bash
cd updateTitles
python update_title.py
```

---

## 4. deleteLecture — Bulk lecture deleter

⚠️  **Destructive — cannot be undone.** Requires explicit `DELETE <count>` confirmation typed at the terminal before any deletion happens.

**Required CSV column:**


| Column       | Description                                     |
| ------------ | ----------------------------------------------- |
| `lecture_id` | Numeric lecture ID (URL is built automatically) |


**Run:**

```bash
cd deleteLecture
python delete_lecture.py
```

For each row: navigates to the detail page, clicks the red trash icon, waits for the confirmation modal, clicks the modal's red Delete button, verifies the modal closes / page redirects.

---

## 5. updateSection — Bulk section updater

Updates section settings via the section edit modal.

**Required CSV column:** `section_id`

**Optional CSV columns** (leave blank to skip per-row):


| Column                 | Description                       |
| ---------------------- | --------------------------------- |
| `section_display_name` | Text input — section display name |
| `type`                 | Dropdown — section type           |
| `course`               | Dropdown — course                 |
| `course_type`          | Dropdown — course type            |
| `flag`                 | Dropdown — flag/status            |
| `module`               | Dropdown — module                 |
| `enable_zoom_web_view` | Dropdown — `Yes` / `No`           |


Other CSV columns (e.g. `name`) are ignored — kept for reference.

**Run:**

```bash
cd updateSection
python update_section.py
```

The script navigates to `/sections/?page=0&section_id=<ID>`, clicks Edit, updates the listed fields, then clicks Save Changes (only if at least one field actually changed).

---

## 6. addMenu — Bulk menu-item creator

Creates menu items under `experience-admin.masaischool.com/menu/`. Data model: every menu item is a *category*; a category can contain other categories or values.

**Required CSV columns:**


| Column           | Description                                                       |
| ---------------- | ----------------------------------------------------------------- |
| `outer_category` | Parent category (e.g. `tickets-category`)                         |
| `inner_category` | Category that holds the value (e.g. `Lecture & Attendance query`) |
| `value`          | Menu-item text to create inside `inner_category`                  |


**Two-phase workflow:**

- **Phase 1** — for each unique `(outer, inner)` pair: check (via menu-page search) whether `inner` exists. If not, open `outer` and create `inner` inside it.
- **Phase 2** — for each row: open `inner` via search and create `value` inside (consecutive same-inner rows skip re-navigation).

**Run:**

```bash
cd addMenu
python add_menu.py
```

---

## 7. updateStudentCode — Bulk student-code (UserName) updater

Updates the LMS `UserName` field for users on `experience-admin.masaischool.com/Users/`.

**CSV columns** (flexible header matching — case/spaces/underscores ignored):


| Column             | Required         | Description                                  |
| ------------------ | ---------------- | -------------------------------------------- |
| `email`            | one of these two | Email used to search the user (preferred)    |
| `Old Student code` | one of these two | Existing UserName — used if `email` is blank |
| `new student code` | yes              | New UserName to set                          |
| `Name`             | no               | Kept in the report for readability           |


**Run:**

```bash
cd updateStudentCode
python update_student_code.py
```

**Run multiple CSVs in parallel** — open two terminals, activate the venv in each, run the script, pick a different CSV in each. Each gets its own browser process (no profile lock conflict).

---

## 8. updateMasaiCohorts — Masai cohort updater

Updates cohort settings on [admissions-admin.masaischool.com](https://admissions-admin.masaischool.com).

**Required CSV columns** (`cohort_id` required; all others optional — leave blank to skip):


| Column                       | Description                                       |
| ---------------------------- | ------------------------------------------------- |
| `cohort_id`                  | Numeric cohort ID                                 |
| `batch_id`                   | Batch ID text                                     |
| `hall_ticket_prefix`         | Hall ticket prefix                                |
| `student_prefix`             | Student prefix                                    |
| `foundation_starts`          | Date — any standard format                        |
| `batch_start_date`           | Date — same formats                               |
| `lms_batch_id`               | LMS batch name to search & select                 |
| `lms_section_ids`            | Comma-separated section names (replaces existing) |
| `manager_id`                 | Manager ID                                        |
| `enable_kit`                 | `TRUE` / `FALSE`                                  |
| `disable_welcome_kit_tshirt` | `TRUE` / `FALSE`                                  |


**Run:**

```bash
cd updateMasaiCohorts
python update_cohort.py
```

**Resume from a specific cohort** (e.g. after an interrupted run):

```bash
python update_cohort.py --start-cohort 2007
```

A visible Chrome window opens for the login check (OTP if session expired), then bulk updates run in the same visible window. Session saved to `browser_profile/` — subsequent runs skip the OTP.

---

## 9. updatePrepleafCohorts — Prepleaf cohort updater

Updates cohort settings on [dashboard-admin.prepleaf.com](https://dashboard-admin.prepleaf.com).

**CSV columns:** identical to Masai cohorts above.

**Run:**

```bash
cd updatePrepleafCohorts
python update_cohort.py
# or resume:
python update_cohort.py --start-cohort 53
```

Same OTP login flow as Masai — first run opens a browser, complete login, press ENTER.

---

## 10. deleteUserFromSection — Bulk user removal from sections

⚠️ **Destructive — cannot be undone.** Requires explicit `DELETE <count>` confirmation typed at the terminal before any deletion happens.

Navigates to each section's detail page (`sections/sectiondetail/?sectionId=<ID>`), opens the **Delete users** panel, pastes all student codes as a comma-separated list, and confirms the deletion.

**Required CSV columns:**


| Column         | Description                                                                                                                                                         |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `section_id`   | Numeric section ID                                                                                                                                                  |
| `student_code` | Student codes to remove — either a comma-separated list in one cell (one row per section), or one code per row (rows for the same section are merged automatically) |


**Run:**

```bash
cd /Users/inno/Projects/lectureUpdate/deleteUserFromSection && python delete_user_from_section.py
```

**Resume from a specific section** (e.g. after an interrupted run where first 2 sections are already done):

```bash
python delete_user_from_section.py --start-section 9718
```

The confirmation prompt shows `DELETE <N>` where N = number of **sections** remaining. All codes for a given section are sent in a single batch to the UI.

---

## Output files

Each run produces two files in the tool's `logs/`:


| File                         | Description                                                             |
| ---------------------------- | ----------------------------------------------------------------------- |
| `run_<name>_<timestamp>.log` | Full timestamped terminal output                                        |
| `run_<name>_<timestamp>.csv` | Per-item result: CREATED / CHANGED / SKIPPED / FAILED / ERROR per field |


The input CSV is automatically copied into `logs/archive/` after each run.

---

## Fixing failures

Every run prints a summary like:

```
  Cohorts with failures/errors: 3/116

  ── Failed / Error cohort IDs ─────────────────────────
    [2101]  {'lms_batch_id': 'FAILED', 'lms_section_ids': 'FAILED'}
    [2094]  {'hall_ticket_prefix': 'FAILED'}
    [2096]  {'foundation_starts': 'FAILED'}  — cannot parse date 'Recordings'
  ─────────────────────────────────────────────────────
```

To re-run only the failed rows: create a new CSV with just those rows in the tool's `input/` folder and run again. For cohort tools you can also use `--start-cohort`.

**Common causes**

- **Timeout failures** — transient network/site slowness; safe to re-run.
- **Date parse errors** — wrong value in the CSV (e.g. text instead of a date); fix the cell.
- **Dropdown verify fail** — site dropdown value differs slightly from the CSV; check the exact label on the platform.
- **Host not found (createLecture)** — host email isn't in `hosts.csv`, or the platform's display name doesn't match the configured `Name`. The script falls back to searching by the email's local part — if that still fails, add or correct the host on the platform side.

