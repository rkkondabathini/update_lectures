"""
update_section.py — Bulk section updater
Site: https://experience-admin.masaischool.com/sections/

Required CSV column:
  section_id        — numeric section ID (used in URL)

Optional CSV columns (each blank cell is skipped on that row):
  section_name          — text input "Enter section name" (required field, marked *)
  section_display_name  — text input "Enter section display name"
  type                  — dropdown "Type"
  course                — dropdown "Course"
  course_type           — dropdown "Course Type"
  flag                  — dropdown "Flag"
  module                — dropdown "Module" (label:nth-child(4))
  enable_zoom_web_view  — dropdown "Enable Zoom Web View" (Yes / No)

Other CSV columns (e.g. 'name') are ignored — kept for reference.

Place CSV in ./input/ and run:
  python update_section.py
"""

import re
import os
import sys
import glob
import shutil
import pandas as pd
from datetime import datetime
from playwright.sync_api import sync_playwright

# ── Directories ───────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
INPUT_DIR   = os.path.join(BASE_DIR, "input")
LOGS_DIR    = os.path.join(BASE_DIR, "logs")
ARCHIVE_DIR = os.path.join(LOGS_DIR, "archive")

for d in (INPUT_DIR, LOGS_DIR, ARCHIVE_DIR):
    os.makedirs(d, exist_ok=True)

# ── Credentials / URLs ────────────────────────────────────────────────────────
LOGIN_URL        = "https://experience-admin.masaischool.com/"
SECTION_URL_TMPL = "https://experience-admin.masaischool.com/sections/?page=0&section_id={section_id}"
EMAIL            = "ravi.kiran@masaischool.com"
PASSWORD         = "mAs@!4321"

# ── Status constants ──────────────────────────────────────────────────────────
SKIPPED = "SKIPPED"
CHANGED = "CHANGED"
FAILED  = "FAILED"
ERROR   = "ERROR"

# Fields tracked in the result CSV (extend when adding more updaters)
RESULT_FIELDS = [
    "section_name", "section_display_name", "type", "course", "course_type", "flag", "module",
    "enable_zoom_web_view",
]


# ── Tee logger ────────────────────────────────────────────────────────────────
class _Tee:
    def __init__(self, filepath):
        self._file    = open(filepath, "w", buffering=1, encoding="utf-8")
        self._stdout  = sys.stdout
        self._pending = ""

    def write(self, data):
        self._stdout.write(data)
        self._pending += data
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._file.write(f"{ts} | {line}\n")

    def flush(self):
        self._stdout.flush()
        self._file.flush()

    def close(self):
        if self._pending:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._file.write(f"{ts} | {self._pending}\n")
        self._file.close()

    def __getattr__(self, name):
        return getattr(self._stdout, name)


_tee = None


def _start_log(stem: str):
    global _tee
    path = os.path.join(LOGS_DIR, f"{stem}.log")
    _tee = _Tee(path)
    sys.stdout = _tee
    print(f"Log → {path}")


def _stop_log():
    global _tee
    if _tee:
        sys.stdout = _tee._stdout
        _tee.close()
        _tee = None


# ── Helpers ───────────────────────────────────────────────────────────────────
def is_blank(val) -> bool:
    if val is None:
        return True
    try:
        if pd.isna(val):
            return True
    except Exception:
        pass
    return str(val).strip() == ""


# ── Modal field updaters ──────────────────────────────────────────────────────
# Each updater returns one of CHANGED / SKIPPED / FAILED.
# Add new updaters here as more fields are needed.
#
# ─────────────────────────────────────────────────────────────────────────────
# REFERENCE: All known section modal field selectors (from playwright codegen).
# Use these when extending the script with new field updaters.
# ─────────────────────────────────────────────────────────────────────────────
#
# TEXT INPUTS
#   Section name           : page.get_by_role("textbox", name="Enter section name")
#   Section display name   : page.get_by_role("textbox", name="Enter section display name")
#   Attendance percentage  : page.get_by_placeholder("Enter attendance percentage (")
#   Allowed minutes        : page.get_by_placeholder("Enter allowed minutes")
#   Minimum video watch    : page.get_by_placeholder("Enter minimum video watch")
#   Catch up days          : page.get_by_placeholder("Enter catch up days")
#   Notes (textarea)       : page.locator("textarea")
#
# REACT-SELECT DROPDOWNS — by id
#   Batch                  : "#batch > .react-select__control > .react-select__value-container > .react-select__input-container"
#   Block                  : "#block > .react-select__control > .react-select__value-container > .react-select__input-container"
#
# REACT-SELECT DROPDOWNS — by label position (label:nth-child(N))
#   Module (4th label)     : "label:nth-child(4) > .react-select > .react-select__control > .react-select__value-container > .react-select__input-container"
#   7th label              : "label:nth-child(7) > .react-select > ..."
#   8th label              : "label:nth-child(8) > .react-select > ..."
#
# REACT-SELECT DROPDOWNS — inside .grid.md:grid-cols-3 (4 dropdowns in 3-col grid)
#   1st: ".grid.md\\:grid-cols-3 > label > .react-select > ... > .react-select__input-container"
#   2nd: ".grid.md\\:grid-cols-3 > label:nth-child(2) > .react-select > ..."
#   3rd: ".grid.md\\:grid-cols-3 > label:nth-child(3) > .react-select > ..."
#   4th: ".grid.md\\:grid-cols-3 > label:nth-child(4) > .react-select > ..."
#
# OTHER FIELDS
#   Attachment             : page.get_by_text("Attachment browse to attach")
#   div:nth-child(11) lbl  : "div:nth-child(11) > label > .react-select > ..."
#
# DROPDOWN OPTION SELECTION (after typing into a react-select)
#   page.get_by_text("<option text>", exact=True).click()
#   OR
#   page.locator(".react-select__option").filter(has_text="<option text>").first.click()
#
# SAVE / CANCEL
#   Save                   : page.get_by_role("button", name="Save Changes")
#
# ─────────────────────────────────────────────────────────────────────────────

def _select_typed_option(page, desired: str, field_name: str) -> str:
    """After a react-select dropdown is open and we've typed `desired`, find and
    click the option whose text matches (case-insensitive). Falls back to first."""
    desired_lower = str(desired).strip().lower()
    options = page.locator(".react-select__option")
    chosen = None
    for i in range(options.count()):
        opt = options.nth(i)
        try:
            if opt.is_visible(timeout=300) and opt.inner_text().strip().lower() == desired_lower:
                chosen = opt
                break
        except Exception:
            continue
    if chosen is None and options.count() > 0:
        chosen = options.first

    if chosen and chosen.is_visible(timeout=500):
        chosen.click()
        page.wait_for_timeout(400)
        return CHANGED

    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    print(f"     [WARN] {field_name}: no option found for '{desired}'")
    return FAILED


def _open_dropdown_by_label_text(page, label_text: str) -> bool:
    """Find a label whose text matches `label_text` (case-insensitive, ignoring
    trailing '*' for required fields) and click its react-select input."""
    return page.evaluate("""(args) => {
        const target = args.text.trim().toLowerCase();
        const labels = [...document.querySelectorAll('label')];
        // Try exact match first (with/without trailing asterisk)
        let lbl = labels.find(l => {
            const t = l.textContent.trim().replace(/\\*$/, '').trim().toLowerCase();
            return t === target;
        });
        // Fall back to a startsWith match
        if (!lbl) {
            lbl = labels.find(l => {
                const t = l.textContent.trim().replace(/\\*$/, '').trim().toLowerCase();
                return t.startsWith(target);
            });
        }
        if (!lbl) return false;
        const input = lbl.querySelector('.react-select__input-container');
        if (!input) return false;
        input.click();
        return true;
    }""", {"text": label_text})


def _update_modal_dropdown_by_label(page, label_text: str, desired: str, field_name: str) -> str:
    """Update a react-select dropdown by finding it via its label text."""
    if is_blank(desired):
        return SKIPPED
    try:
        if not _open_dropdown_by_label_text(page, label_text):
            print(f"     [WARN] {field_name}: dropdown labeled '{label_text}' not found")
            return FAILED
        page.wait_for_timeout(500)
        page.keyboard.type(str(desired), delay=50)
        page.wait_for_timeout(800)
        return _select_typed_option(page, desired, field_name)
    except Exception as e:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        print(f"     [WARN] {field_name} update failed: {e}")
        return FAILED


# ── Specific field updaters ──────────────────────────────────────────────────

def _update_modal_section_name(page, desired: str) -> str:
    """Update the 'Enter section name' text input (required field, marked *)."""
    if is_blank(desired):
        return SKIPPED
    try:
        box = page.get_by_role("textbox", name="Enter section name")
        box.click()
        box.fill(str(desired))
        page.wait_for_timeout(300)
        return CHANGED
    except Exception as e:
        print(f"     [WARN] Section name update failed: {e}")
        return FAILED


def _update_modal_section_display_name(page, desired: str) -> str:
    """Update the 'Enter section display name' text input."""
    if is_blank(desired):
        return SKIPPED
    try:
        box = page.get_by_role("textbox", name="Enter section display name")
        box.click()
        box.fill(str(desired))
        page.wait_for_timeout(300)
        return CHANGED
    except Exception as e:
        print(f"     [WARN] Section display name update failed: {e}")
        return FAILED


def _update_modal_module(page, desired: str) -> str:
    """Update the Module dropdown (4th label react-select in the modal — confirmed)."""
    if is_blank(desired):
        return SKIPPED
    try:
        module_input = page.locator(
            "label:nth-child(4) > .react-select > .react-select__control > "
            ".react-select__value-container > .react-select__input-container"
        ).first
        module_input.click()
        page.wait_for_timeout(400)
        page.keyboard.type(str(desired), delay=50)
        page.wait_for_timeout(800)
        return _select_typed_option(page, desired, "Module")
    except Exception as e:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        print(f"     [WARN] Module update failed: {e}")
        return FAILED


def _update_modal_type(page, desired: str) -> str:
    return _update_modal_dropdown_by_label(page, "Type", desired, "Type")


def _update_modal_course(page, desired: str) -> str:
    return _update_modal_dropdown_by_label(page, "Course", desired, "Course")


def _update_modal_course_type(page, desired: str) -> str:
    return _update_modal_dropdown_by_label(page, "Course Type", desired, "Course Type")


def _update_modal_flag(page, desired: str) -> str:
    return _update_modal_dropdown_by_label(page, "Flag", desired, "Flag")


def _update_modal_enable_zoom_web_view(page, desired: str) -> str:
    return _update_modal_dropdown_by_label(
        page, "Enable Zoom Web View", desired, "Enable Zoom Web View"
    )


# ── Per-section processor ────────────────────────────────────────────────────
def process_section(page, row) -> dict:
    section_id = str(row.get("section_id", "")).strip()
    s = {"section_id": section_id, "save": SKIPPED, "notes": ""}
    for f in RESULT_FIELDS:
        s[f] = SKIPPED

    if is_blank(section_id):
        s["notes"] = "section_id is blank"
        return s

    # Navigate directly to the section by ID
    url = SECTION_URL_TMPL.format(section_id=section_id)
    print(f"  Loading: {url}")
    page.goto(url)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1_500)

    # Click Edit on the row. The Action column now also contains a 'Copy' link
    # alongside 'Edit', so we target the exact "Edit" text/link rather than the
    # cell. Try link role → text match → cell as progressive fallbacks.
    edit_locator = page.get_by_role("link", name=re.compile(r"^Edit$", re.I))
    if edit_locator.count() == 0:
        edit_locator = page.get_by_text("Edit", exact=True)
    if edit_locator.count() == 0:
        edit_locator = page.get_by_role("cell", name="Edit")
    if edit_locator.count() == 0:
        print(f"  [WARN] No Edit element — section_id '{section_id}' not found")
        s["notes"]  = "Section not found"
        s["module"] = FAILED
        return s

    try:
        edit_locator.first.click()
        page.wait_for_timeout(1_000)
    except Exception as e:
        print(f"  [WARN] Failed to click Edit: {e}")
        s["notes"]  = f"Edit click failed: {e}"
        s["module"] = ERROR
        return s

    # Apply field updates — order: text fields first, then dropdowns
    print(f"  1. Section name → '{str(row.get('section_name','')).strip()}'")
    s["section_name"] = _update_modal_section_name(page, row.get("section_name", ""))

    print(f"  2. Section display name → '{str(row.get('section_display_name','')).strip()}'")
    s["section_display_name"] = _update_modal_section_display_name(page, row.get("section_display_name", ""))

    print(f"  3. Type        → '{str(row.get('type','')).strip()}'")
    s["type"] = _update_modal_type(page, row.get("type", ""))

    print(f"  4. Course      → '{str(row.get('course','')).strip()}'")
    s["course"] = _update_modal_course(page, row.get("course", ""))

    print(f"  5. Course Type → '{str(row.get('course_type','')).strip()}'")
    s["course_type"] = _update_modal_course_type(page, row.get("course_type", ""))

    print(f"  6. Flag        → '{str(row.get('flag','')).strip()}'")
    s["flag"] = _update_modal_flag(page, row.get("flag", ""))

    print(f"  7. Module      → '{str(row.get('module','')).strip()}'")
    s["module"] = _update_modal_module(page, row.get("module", ""))

    print(f"  8. Enable Zoom Web View → '{str(row.get('enable_zoom_web_view','')).strip()}'")
    s["enable_zoom_web_view"] = _update_modal_enable_zoom_web_view(
        page, row.get("enable_zoom_web_view", "")
    )

    # Save if anything was actually CHANGED
    if any(s[f] == CHANGED for f in RESULT_FIELDS):
        try:
            page.get_by_role("button", name="Save Changes").click()
            page.wait_for_timeout(1_500)
            s["save"] = CHANGED
            print(f"  [SAVED]")
        except Exception as e:
            s["save"]   = FAILED
            s["notes"] += f" | Save error: {e}"
            print(f"  [SAVE FAILED] {e}")
    else:
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        except Exception:
            pass
        s["save"] = SKIPPED

    return s


# ── Entry point ──────────────────────────────────────────────────────────────
def run():
    csv_files = sorted(glob.glob(os.path.join(INPUT_DIR, "*.csv")))
    if not csv_files:
        print(f"[ERROR] No CSV files found in {INPUT_DIR}/")
        return

    print(f"Found {len(csv_files)} CSV file(s):")
    for i, f in enumerate(csv_files):
        print(f"  [{i}] {os.path.basename(f)}")

    if len(csv_files) == 1:
        chosen = csv_files[0]
        print(f"Auto-selecting: {os.path.basename(chosen)}")
    else:
        idx = input("\nEnter file number: ").strip()
        try:
            chosen = csv_files[int(idx)]
        except (ValueError, IndexError):
            print("[ERROR] Invalid selection.")
            return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base      = os.path.splitext(os.path.basename(chosen))[0]
    log_stem  = f"run_{base}_{timestamp}"
    _start_log(log_stem)

    df = pd.read_csv(chosen, dtype=str)
    if "section_id" not in df.columns:
        print("[ERROR] CSV must have a 'section_id' column")
        _stop_log()
        return

    print(f"Rows to process: {len(df)}\n")
    all_results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            slow_mo=400,
            args=["--start-maximized"],
        )
        context = browser.new_context(no_viewport=True)
        page    = context.new_page()
        page.bring_to_front()

        # Login
        print("Attempting auto-login...")
        try:
            page.goto(LOGIN_URL)
            page.wait_for_load_state("networkidle")
            page.get_by_role("textbox", name="Your email").fill(EMAIL)
            page.get_by_role("textbox", name="Your email").press("Tab")
            page.get_by_role("textbox", name="Your password").fill(PASSWORD)
            page.locator("svg").click()
            page.get_by_role("button", name="Sign In").click()
            page.wait_for_load_state("networkidle", timeout=20_000)
            if "login" in page.url.lower() or page.url.rstrip("/") == LOGIN_URL.rstrip("/"):
                raise Exception(f"Still on login page: {page.url}")
            print("Logged in.\n")
        except Exception as login_err:
            print(f"[WARN] Auto-login failed: {login_err}")
            input("Please log in manually, then press ENTER... ")
            print("Resuming...\n")

        for i, row in df.iterrows():
            print(f"{'─'*60}")
            section_id = str(row.get("section_id", "")).strip()
            print(f"[{i+1}/{len(df)}] section_id={section_id}")
            try:
                result = process_section(page, row)
            except Exception as e:
                print(f"  [ERROR] {e}")
                result = {"section_id": section_id, "save": ERROR, "notes": str(e)}
                for f in RESULT_FIELDS:
                    result[f] = ERROR
            all_results.append(result)
            print()

        browser.close()

    csv_path = os.path.join(LOGS_DIR, f"{log_stem}.csv")
    pd.DataFrame(all_results).to_csv(csv_path, index=False)
    print(f"\nCSV report → {csv_path}")

    dest = os.path.join(ARCHIVE_DIR, f"{base}_{timestamp}.csv")
    shutil.copy2(chosen, dest)
    print(f"Input archived → {dest}")

    df_log = pd.DataFrame(all_results)
    print("\n══ Summary ══════════════════════════════════════════════")
    for col in RESULT_FIELDS + ["save"]:
        if col in df_log.columns:
            print(f"  {col:20s}: {df_log[col].value_counts().to_dict()}")

    skip_keys = {"notes", "section_id"}
    failed = [s for s in all_results
              if any(v in (FAILED, ERROR) for k, v in s.items() if k not in skip_keys)]
    print(f"\n  Sections with failures/errors: {len(failed)}/{len(all_results)}")

    if failed:
        print("\n  ── Failed / Error sections ───────────────────────────")
        for s in failed:
            sid  = s.get("section_id", "")
            bad  = {k: v for k, v in s.items() if k not in skip_keys and v in (FAILED, ERROR)}
            note = s.get("notes", "")
            line = f"    [{sid}]  {bad}"
            if note:
                line += f"  — {note}"
            print(line)
        print("  ─────────────────────────────────────────────────────")

    print("═════════════════════════════════════════════════════════")
    print("Done.")
    _stop_log()


if __name__ == "__main__":
    run()
