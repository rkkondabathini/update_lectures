"""
update_batch.py — Bulk batch updater
Site: https://experience-admin.masaischool.com/batches/edit/?id=<batch_id>

REQUIRED CSV column:
  batch_id

OPTIONAL CSV columns (each updated only when the cell is non-blank;
columns not in the CSV are simply skipped):

  Text inputs:
    name, pp_link, course_title, institute_name

  Date inputs (any common date format):
    batch_start_date  (alias: start_date)   → "Starting"
    batch_end_date    (alias: end_date)     → "Ending"

  React-select dropdowns (we type to filter, then click the option):
    programs, duration_type, mode, model

  Native <select> dropdowns (CSV value must match the <option value="…">):
    status, institute, language, iteration, duration_months,
    program_domain, program_type, collaboration

  Checkboxes (TRUE/FALSE, yes/no, 1/0):
    show_batch_details, show_attendance_report,
    show_evaluation_report, show_masaiverse_community

Place CSV in ./input/ and run:
  python update_batch.py
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
LOGIN_URL    = "https://experience-admin.masaischool.com/"
BATCH_URL    = "https://experience-admin.masaischool.com/batches/edit/?id={batch_id}"
EMAIL        = "ravi.kiran@masaischool.com"
PASSWORD     = "mAs@!4321"

# ── Status constants ──────────────────────────────────────────────────────────
SKIPPED = "SKIPPED"
CHANGED = "CHANGED"
FAILED  = "FAILED"
ERROR   = "ERROR"


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


def to_iso_date(val):
    """Parse common date strings → 'YYYY-MM-DD'. Returns None if unparsable."""
    if is_blank(val):
        return None
    s = str(val).strip()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%Y/%m/%d",
                "%d/%m/%y", "%d-%m-%y", "%d %b %Y", "%d %B %Y",
                "%a, %b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    try:
        return pd.to_datetime(s, dayfirst=True).strftime("%Y-%m-%d")
    except Exception:
        return None


def _to_bool(val):
    s = str(val).strip().lower()
    if s in ("true", "yes", "y", "1", "on", "checked"):
        return True
    if s in ("false", "no", "n", "0", "off", "unchecked"):
        return False
    return None


def _clean(val):
    """Strip CSV-escape leftover quotes and whitespace."""
    s = str(val).strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1].strip()
    return s


# ─────────────────────────────────────────────────────────────────────────────
# Field handlers — each returns CHANGED / SKIPPED / FAILED
# Built directly from the Playwright codegen recording the user captured, so
# selectors match the platform exactly.
# ─────────────────────────────────────────────────────────────────────────────

def _set_textbox(page, ui_name: str, value) -> str:
    if is_blank(value):
        return SKIPPED
    v = _clean(value)
    try:
        tb = page.get_by_role("textbox", name=ui_name)
        if tb.count() == 0:
            print(f"     [WARN] textbox '{ui_name}' not found")
            return FAILED
        tb = tb.first
        current = (tb.input_value() or "").strip()
        if current == v:
            print(f"     SKIP (already '{current}')")
            return SKIPPED
        tb.click()
        try:
            tb.press("ControlOrMeta+a")
        except Exception:
            pass
        tb.fill(v)
        page.wait_for_timeout(200)
        print(f"     [OK] '{current or 'empty'}' → '{v}'")
        return CHANGED
    except Exception as e:
        print(f"     [WARN] textbox '{ui_name}' update failed: {e}")
        return FAILED


def _set_date(page, ui_name: str, value) -> str:
    if is_blank(value):
        return SKIPPED
    iso = to_iso_date(value)
    if iso is None:
        print(f"     [WARN] {ui_name}: cannot parse date '{value}'")
        return FAILED
    try:
        tb = page.get_by_role("textbox", name=ui_name)
        if tb.count() == 0:
            print(f"     [WARN] date input '{ui_name}' not found")
            return FAILED
        # Use the React-compatible prototype setter so the value sticks
        result = tb.first.evaluate("""(inp, args) => {
            const current = inp.value || '';
            if (current === args.value) return {skipped: true, current};
            const setter = Object.getOwnPropertyDescriptor(
                HTMLInputElement.prototype, 'value'
            ).set;
            setter.call(inp, args.value);
            inp.dispatchEvent(new Event('input',  {bubbles: true}));
            inp.dispatchEvent(new Event('change', {bubbles: true}));
            inp.dispatchEvent(new Event('blur',   {bubbles: true}));
            return {skipped: false, previous: current};
        }""", {"value": iso})
        if result.get("skipped"):
            print(f"     SKIP (already '{result.get('current')}')")
            return SKIPPED
        page.wait_for_timeout(200)
        prev = result.get("previous", "")
        print(f"     [OK] '{prev or 'empty'}' → '{iso}'")
        return CHANGED
    except Exception as e:
        print(f"     [WARN] {ui_name} update failed: {e}")
        return FAILED


def _set_native_select(page, ui_name: str, value) -> str:
    """Native <select>. Codegen pattern: page.get_by_label("Label...placeholder").select_option("VALUE")
    The accessible name of these selects is "Label" + the current placeholder
    or option text, so a prefix-only label match is the safest matcher."""
    if is_blank(value):
        return SKIPPED
    v = _clean(value)
    try:
        # Match by label-prefix (case-insensitive). select_option ignores trailing
        # placeholder text in the accessible name.
        pattern = re.compile(r"^\s*" + re.escape(ui_name), re.I)
        sel = page.get_by_label(pattern)
        if sel.count() == 0:
            print(f"     [WARN] native select '{ui_name}' not found")
            return FAILED
        sel = sel.first
        # Read current to detect SKIP
        try:
            current = (sel.evaluate("(s) => s.value || ''") or "").strip()
            current_text = (sel.evaluate(
                "(s) => (s.options[s.selectedIndex] || {}).textContent || ''"
            ) or "").strip()
            if current == v or current_text.lower() == v.lower():
                print(f"     SKIP (already '{current_text or current}')")
                return SKIPPED
        except Exception:
            current, current_text = "", ""

        # Try by value, then by label
        try:
            sel.select_option(value=v)
        except Exception:
            try:
                sel.select_option(label=v)
            except Exception as e:
                print(f"     [WARN] native select '{ui_name}': cannot select '{v}': {e}")
                return FAILED
        page.wait_for_timeout(200)
        print(f"     [OK] '{current_text or 'empty'}' → '{v}'")
        return CHANGED
    except Exception as e:
        print(f"     [WARN] native select '{ui_name}' update failed: {e}")
        return FAILED


def _set_react_select(page, ui_name: str, value) -> str:
    """React-select. Codegen pattern:
        page.get_by_role("combobox", name="Model option …").fill("model-1")
        page.get_by_text("model-1", exact=True).click()
    We match the combobox by the prefix of its accessible name (which is the
    label), then type-to-filter and click the matching option."""
    if is_blank(value):
        return SKIPPED
    v = _clean(value)
    try:
        pattern = re.compile(r"^\s*" + re.escape(ui_name), re.I)
        combo = page.get_by_role("combobox", name=pattern)
        if combo.count() == 0:
            print(f"     [WARN] combobox '{ui_name}' not found")
            return FAILED
        combo = combo.first

        # Check current via accessible name (it includes the selected value)
        try:
            aname = (combo.get_attribute("aria-label") or "")
            if v.lower() in aname.lower() and "selected" in aname.lower():
                print(f"     SKIP (already '{v}')")
                return SKIPPED
        except Exception:
            pass

        combo.click()
        page.wait_for_timeout(300)
        try:
            combo.fill(v[:30])
        except Exception:
            try:
                page.keyboard.type(v[:30], delay=20)
            except Exception:
                pass
        page.wait_for_timeout(400)

        # Pick option: exact text → contains → first visible
        option = None
        try:
            exact = page.locator(".react-select__option").filter(
                has_text=re.compile(r"^\s*" + re.escape(v) + r"\s*$", re.I))
            if exact.count() > 0 and exact.first.is_visible(timeout=600):
                option = exact.first
        except Exception:
            pass
        if option is None:
            try:
                contains = page.locator(".react-select__option").filter(
                    has_text=re.compile(re.escape(v), re.I))
                if contains.count() > 0 and contains.first.is_visible(timeout=600):
                    option = contains.first
            except Exception:
                pass
        if option is None:
            try:
                first = page.locator(".react-select__option").first
                if first.count() > 0 and first.is_visible(timeout=600):
                    option = first
            except Exception:
                pass

        if option is None:
            print(f"     [WARN] '{ui_name}': no option matching '{v}'")
            try: page.keyboard.press("Escape")
            except Exception: pass
            return FAILED

        opt_text = (option.text_content() or "").strip()
        option.click()
        page.wait_for_timeout(300)
        print(f"     [OK] → '{opt_text}'")
        return CHANGED
    except Exception as e:
        print(f"     [WARN] '{ui_name}' update failed: {e}")
        try: page.keyboard.press("Escape")
        except Exception: pass
        return FAILED


def _set_checkbox(page, ui_name: str, value) -> str:
    if is_blank(value):
        return SKIPPED
    desired = _to_bool(value)
    if desired is None:
        print(f"     [WARN] checkbox '{ui_name}': cannot interpret '{value}'")
        return FAILED
    try:
        cb = page.get_by_role("checkbox", name=ui_name)
        if cb.count() == 0:
            print(f"     [WARN] checkbox '{ui_name}' not found")
            return FAILED
        cb = cb.first
        current = cb.is_checked()
        if current == desired:
            print(f"     SKIP (already {'checked' if current else 'unchecked'})")
            return SKIPPED
        if desired:
            cb.check()
        else:
            cb.uncheck()
        page.wait_for_timeout(200)
        print(f"     [OK] → {'checked' if desired else 'unchecked'}")
        return CHANGED
    except Exception as e:
        print(f"     [WARN] checkbox '{ui_name}' update failed: {e}")
        return FAILED


# ─────────────────────────────────────────────────────────────────────────────
# Field registry  — single source of truth for which CSV columns map to which
# UI fields and which handler. Order matters: visually top-to-bottom on the
# page, which avoids re-rendering bouncing focus around.
# ─────────────────────────────────────────────────────────────────────────────

# (csv_col, ui_name, kind)
FIELD_REGISTRY = [
    ("name",                      "Name",                      "text"),
    ("programs",                  "Programs",                  "react_select"),
    ("duration_type",             "Duration Type",             "react_select"),
    ("batch_start_date",          "Starting",                  "date"),
    ("start_date",                "Starting",                  "date"),     # alias
    ("batch_end_date",            "Ending",                    "date"),
    ("end_date",                  "Ending",                    "date"),     # alias
    ("mode",                      "Mode",                      "react_select"),
    ("model",                     "Model",                     "react_select"),
    ("status",                    "Status",                    "native_select"),
    ("institute",                 "Institute",                 "native_select"),
    ("language",                  "Language",                  "native_select"),
    ("iteration",                 "Iteration",                 "native_select"),
    ("duration_months",           "Duration (Months)",         "native_select"),
    ("program_domain",            "Program Domain",            "native_select"),
    ("program_type",              "Program Type",              "native_select"),
    ("collaboration",             "Collaboration",             "native_select"),
    ("pp_link",                   "PP Link",                   "text"),
    ("show_batch_details",        "Show Batch Details",        "checkbox"),
    ("show_attendance_report",    "Show Attendance Report",    "checkbox"),
    ("show_evaluation_report",    "Show Evaluation Report",    "checkbox"),
    ("show_masaiverse_community", "Show MasaiVerse Community", "checkbox"),
    ("course_title",              "Course Title",              "text"),
    ("institute_name",            "Institute Name",            "text"),
]

# Distinct status-tracked fields (de-dupe aliases that target the same UI field)
RESULT_FIELDS = []
_seen_targets = set()
for csv_col, ui_name, kind in FIELD_REGISTRY:
    key = (ui_name, kind)
    if key in _seen_targets:
        continue
    _seen_targets.add(key)
    RESULT_FIELDS.append(csv_col)


def _dispatch(page, csv_col: str, ui_name: str, kind: str, value) -> str:
    if kind == "text":
        return _set_textbox(page, ui_name, value)
    if kind == "date":
        return _set_date(page, ui_name, value)
    if kind == "native_select":
        return _set_native_select(page, ui_name, value)
    if kind == "react_select":
        return _set_react_select(page, ui_name, value)
    if kind == "checkbox":
        return _set_checkbox(page, ui_name, value)
    print(f"     [WARN] unknown field kind '{kind}' for column '{csv_col}'")
    return FAILED


# ── Save ─────────────────────────────────────────────────────────────────────
def _save(page) -> str:
    try:
        btn = page.get_by_role("button", name="Save Changes")
        if btn.count() == 0 or not btn.first.is_visible(timeout=500):
            print(f"     [WARN] Save: 'Save Changes' button not found")
            return FAILED
        btn.first.click()
        page.wait_for_timeout(1_000)
        # Dismiss the "Okay" confirmation modal if it appears
        try:
            okay = page.get_by_role("button", name="Okay")
            if okay.count() > 0 and okay.first.is_visible(timeout=2_000):
                okay.first.click()
                page.wait_for_timeout(500)
        except Exception:
            pass
        return CHANGED
    except Exception as e:
        print(f"     [WARN] Save failed: {e}")
        return FAILED


# ── Per-batch processor ──────────────────────────────────────────────────────
def process_batch(page, row, columns_in_csv) -> dict:
    batch_id = str(row.get("batch_id", "")).strip()
    s = {"batch_id": batch_id, "save": SKIPPED, "notes": ""}
    for f in RESULT_FIELDS:
        s[f] = SKIPPED

    if is_blank(batch_id):
        s["notes"] = "batch_id is blank"
        return s

    url = BATCH_URL.format(batch_id=batch_id)
    print(f"  Loading: {url}")
    page.goto(url)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1_500)

    step = 1
    for csv_col, ui_name, kind in FIELD_REGISTRY:
        if csv_col not in columns_in_csv:
            continue
        raw = row.get(csv_col, "")
        if is_blank(raw):
            continue
        printable = _clean(raw)
        print(f"  {step:>2}. {csv_col:<26s} → '{printable}'")
        status = _dispatch(page, csv_col, ui_name, kind, raw)
        s[csv_col] = status
        step += 1

    # Save only if at least one field actually CHANGED
    if any(v == CHANGED for k, v in s.items() if k in RESULT_FIELDS):
        print(f"  Saving...")
        s["save"] = _save(page)
        if s["save"] == CHANGED:
            print(f"  [SAVED]")
    else:
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
    if "batch_id" not in df.columns:
        print("[ERROR] CSV must have a 'batch_id' column")
        _stop_log()
        return

    known_csv_cols = {c for c, _, _ in FIELD_REGISTRY}
    present = [c for c in df.columns if c in known_csv_cols]
    if not present:
        print(f"[ERROR] CSV has no recognised updatable columns.")
        print(f"        Expected one or more of: {sorted(known_csv_cols)}")
        _stop_log()
        return
    print(f"Updatable columns detected: {present}")

    ignored = [c for c in df.columns if c not in known_csv_cols and c != "batch_id"]
    if ignored:
        print(f"Ignored CSV columns (not used for updates): {ignored}")

    columns_in_csv = set(df.columns)

    print(f"Rows to process: {len(df)}\n")
    all_results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=200)
        context = browser.new_context()
        page    = context.new_page()

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
            batch_id = str(row.get("batch_id", "")).strip()
            print(f"[{i+1}/{len(df)}] batch_id={batch_id}")
            try:
                result = process_batch(page, row, columns_in_csv)
            except Exception as e:
                print(f"  [ERROR] {e}")
                result = {"batch_id": batch_id, "save": ERROR, "notes": str(e)}
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
            vc = df_log[col].value_counts().to_dict()
            # Only show fields that had any non-SKIPPED activity
            if any(k != SKIPPED for k in vc):
                print(f"  {col:28s}: {vc}")

    skip_keys = {"notes", "batch_id"}
    failed = [s for s in all_results
              if any(v in (FAILED, ERROR) for k, v in s.items() if k not in skip_keys)]
    print(f"\n  Batches with failures/errors: {len(failed)}/{len(all_results)}")

    if failed:
        print("\n  ── Failed / Error batch IDs ──────────────────────────")
        for s in failed:
            bid  = s.get("batch_id", "")
            bad  = {k: v for k, v in s.items() if k not in skip_keys and v in (FAILED, ERROR)}
            note = s.get("notes", "")
            line = f"    [{bid}]  {bad}"
            if note:
                line += f"  — {note}"
            print(line)
        print("  ─────────────────────────────────────────────────────")

    print("═════════════════════════════════════════════════════════")
    print("Done.")
    _stop_log()


if __name__ == "__main__":
    run()
