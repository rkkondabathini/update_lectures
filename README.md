# LectureUpdate — Automation Scripts

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
│
├── updateMasaiCohorts/    Masai cohort settings updater
└── updatePrepleafCohorts/ Prepleaf (iHub) cohort settings updater
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
| Column | Description |
|--------|-------------|
| `lecture_url` | Full URL of the lecture edit page |
| `updated_category` | New category |
| `updated_module` | New module |
| `updated_tags` | Comma-separated tags |
| `updated_mandatory` | `TRUE` / `FALSE` |
| `updated_show_feedback` | `TRUE` / `FALSE` |

**Run:**
```bash
cd updateLecture
python update_lecture.py
```

For each row: reads current DOM values, skips fields already correct, updates the rest, verifies (retries once on mismatch), then saves.

---

## 3. updateTitles — Bulk title updater

**Required CSV columns:**
| Column | Description |
|--------|-------------|
| `lecture_url` | Full URL of the lecture edit page |
| `updated_title` | New title text |

**Run:**
```bash
cd updateTitles
python update_title.py
```

---

## 4. deleteLecture — Bulk lecture deleter

⚠️  **Destructive — cannot be undone.** Requires explicit `DELETE <count>` confirmation typed at the terminal before any deletion happens.

**Required CSV column:**
| Column | Description |
|--------|-------------|
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
| Column | Description |
|--------|-------------|
| `section_display_name` | Text input — section display name |
| `type` | Dropdown — section type |
| `course` | Dropdown — course |
| `course_type` | Dropdown — course type |
| `flag` | Dropdown — flag/status |
| `module` | Dropdown — module |

Other CSV columns (e.g. `name`) are ignored — kept for reference.

**Run:**
```bash
cd updateSection
python update_section.py
```

The script navigates to `/sections/?page=0&section_id=<ID>`, clicks Edit, updates the listed fields, then clicks Save Changes (only if at least one field actually changed).

---

## 6. updateMasaiCohorts — Masai cohort updater

Updates cohort settings on [admissions-admin.masaischool.com](https://admissions-admin.masaischool.com).

**Required CSV columns** (`cohort_id` required; all others optional — leave blank to skip):
| Column | Description |
|--------|-------------|
| `cohort_id` | Numeric cohort ID |
| `batch_id` | Batch ID text |
| `hall_ticket_prefix` | Hall ticket prefix |
| `student_prefix` | Student prefix |
| `foundation_starts` | Date — any standard format |
| `batch_start_date` | Date — same formats |
| `lms_batch_id` | LMS batch name to search & select |
| `lms_section_ids` | Comma-separated section names (replaces existing) |
| `manager_id` | Manager ID |
| `enable_kit` | `TRUE` / `FALSE` |
| `disable_welcome_kit_tshirt` | `TRUE` / `FALSE` |

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

## 7. updatePrepleafCohorts — Prepleaf cohort updater

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

## Output files

Each run produces two files in the tool's `logs/`:

| File | Description |
|------|-------------|
| `run_<name>_<timestamp>.log` | Full timestamped terminal output |
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
