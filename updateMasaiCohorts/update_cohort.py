"""
update_cohort.py — Masai cohort management updater
Site: https://admissions-admin.masaischool.com/iit/cohort-management

CSV columns (cohort_id required; all others optional — leave blank to skip):
  cohort_id, batch_id, hall_ticket_prefix, student_prefix,
  foundation_starts, batch_start_date,
  lms_batch_id                       — Whenever this OR any section bucket below is filled in,
                                        the script deliberately swaps to a disposable throwaway
                                        batch (THROWAWAY_BATCH) and back to the target batch
                                        first, to force every section bucket to a guaranteed-
                                        empty state (reselecting Batch ID — even unchanged —
                                        wipes all section buckets, and some buckets don't expose
                                        a removable chip for pre-existing picks, so detecting and
                                        clearing stale selections directly proved unreliable).
                                        Fill in every section bucket you want kept in that same
                                        row — anything left blank stays empty after the reset.
  lms_section_ids                    — LMS Section IDs (Fallback), comma-separated
  lms_section_ids_after_full_fee      — After Full Fee Section IDs, comma-separated
  lms_section_ids_student             — Persona: Student sections, comma-separated
  lms_section_ids_working_professional — Persona: Working Professional sections, comma-separated
  lms_trial_access                   — LMS Trial Access Before Full Fee toggle (TRUE/FALSE)
  lms_section_ids_after_secure_seat   — After Secure Seat sections (only applied when
                                        lms_trial_access is/becomes TRUE — field is hidden
                                        in the UI otherwise), comma-separated
  manager_id, enable_kit, disable_welcome_kit_tshirt

Place input CSV in ./input/ and run:
  python update_cohort.py
  python update_cohort.py --start-cohort 2007
"""

import re
import os
import sys
import glob
import shutil
import pandas as pd
from datetime import datetime
from playwright.sync_api import sync_playwright

DEFAULT_PLATFORM = "masai"

# ── Directories ────────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
INPUT_DIR   = os.path.join(BASE_DIR, "input")
LOGS_DIR    = os.path.join(BASE_DIR, "logs")
ARCHIVE_DIR = os.path.join(LOGS_DIR, "archive")

for d in (INPUT_DIR, LOGS_DIR, ARCHIVE_DIR):
    os.makedirs(d, exist_ok=True)

# ── Platform config ────────────────────────────────────────────────────────────
PLATFORMS = {
    "masai": {
        "base_url":    "https://admissions-admin.masaischool.com/iit/cohort-management",
        "login_url":   "https://admissions-admin.masaischool.com/",
        "profile_dir": os.path.join(BASE_DIR, "browser_profile"),
    },
    "prepleaf": {
        "base_url":    "https://dashboard-admin.prepleaf.com/iit/cohort-management",
        "login_url":   "https://www.ihubiitrcourses.org/signup",
        "profile_dir": os.path.join(BASE_DIR, "browser_profile"),
    },
}

# Back-compat aliases
BASE_URL    = PLATFORMS[DEFAULT_PLATFORM]["base_url"]
LOGIN_URL   = PLATFORMS[DEFAULT_PLATFORM]["login_url"]
PROFILE_DIR = PLATFORMS[DEFAULT_PLATFORM]["profile_dir"]

# ── Status constants ───────────────────────────────────────────────────────────
SKIPPED = "SKIPPED"
CHANGED = "CHANGED"
FAILED  = "FAILED"
ERROR   = "ERROR"

RESULT_FIELDS = [
    "cohort_id", "batch_id", "hall_ticket_prefix", "student_prefix",
    "foundation_starts", "batch_start_date", "lms_batch_id",
    "lms_section_ids", "lms_section_ids_after_full_fee",
    "lms_section_ids_student", "lms_section_ids_working_professional",
    "lms_trial_access", "lms_section_ids_after_secure_seat",
    "manager_id", "enable_kit", "disable_welcome_kit_tshirt", "notes",
]
SUMMARY_FIELDS = RESULT_FIELDS[1:-1]


# ── Tee logger ─────────────────────────────────────────────────────────────────
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


# ── Helpers ────────────────────────────────────────────────────────────────────
def is_empty(val) -> bool:
    if val is None:
        return True
    try:
        if pd.isna(val):
            return True
    except Exception:
        pass
    return str(val).strip() == ""


def to_bool(val):
    if is_empty(val):
        return None
    s = str(val).strip().upper()
    if s in ("TRUE", "YES", "1"):
        return True
    if s in ("FALSE", "NO", "0"):
        return False
    return None


def parse_dt(val: str):
    val = str(val).strip()
    for fmt in (
        "%d/%m/%Y %H:%M", "%d/%m/%Y",
        "%d-%m-%Y %H:%M", "%d-%m-%Y",
        "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d",
        "%d %b %Y %H:%M", "%d %b %Y",
        "%d %B %Y %H:%M", "%d %B %Y",
        "%m/%d/%Y %H:%M", "%m/%d/%Y",
    ):
        try:
            return datetime.strptime(val, fmt).strftime("%Y-%m-%dT%H:%M")
        except ValueError:
            pass
    try:
        return pd.to_datetime(val, dayfirst=True).strftime("%Y-%m-%dT%H:%M")
    except Exception:
        pass
    return None


def dt_display(val: str) -> str:
    try:
        return datetime.strptime(val.strip(), "%Y-%m-%dT%H:%M").strftime("%d/%m/%Y %H:%M")
    except Exception:
        return val.strip()


def _dismiss_dialog(page):
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
    except Exception:
        pass


_TOAST_SUCCESS_PAT = re.compile(
    r"(LMS settings saved|Field updated successfully|Important date updated|all.*(changes|updates).*(done|saved|complete))",
    re.I,
)
_TOAST_ERROR_PAT = re.compile(r"(error|failed|invalid|please select)", re.I)

# The LMS Settings card has a PERMANENT static reminder banner containing the
# word "required" — confirmed live that broadening the error pattern to include
# "required" made every single save falsely match this static text as if it
# were an error toast, even on a real success. Anything containing this phrase
# is page furniture, never an actual notification, and must be excluded.
_STATIC_NOTE_TEXT = "at least one Section ID, and Manager ID are all required"


def _wait_for_toast(page, timeout_ms: int = 4000):
    """Poll briefly for the top-right save-confirmation/error toast instead of
    assuming a button click succeeded just because it didn't raise — confirmed
    live that the UI can show an error toast (e.g. "Error updating field")
    while a click still goes through cleanly with no exception.
    Returns (True, text) on a recognized success toast, (False, text) on a
    recognized error toast, or (None, "") if no toast appeared in time
    (inconclusive — caller should not assume success)."""
    elapsed = 0
    step = 250
    while elapsed <= timeout_ms:
        # Prefer real ARIA notification roles — toasts are almost always built
        # with role="status"/"alert" for accessibility, which the page's own
        # static helper text would not carry, sidestepping the collision above
        # entirely rather than relying only on excluding known static text.
        role_toast = page.locator("[role='status'], [role='alert']")
        for i in range(role_toast.count()):
            try:
                txt = role_toast.nth(i).inner_text().strip()
            except Exception:
                continue
            if not txt or _STATIC_NOTE_TEXT in txt:
                continue
            if _TOAST_ERROR_PAT.search(txt):
                return False, txt
            if _TOAST_SUCCESS_PAT.search(txt):
                return True, txt
            return None, txt  # a real toast, just not one of our known phrasings

        err = page.get_by_text(_TOAST_ERROR_PAT)
        for i in range(err.count()):
            try:
                txt = err.nth(i).inner_text().strip()
            except Exception:
                continue
            if _STATIC_NOTE_TEXT in txt:
                continue
            return False, txt

        ok = page.get_by_text(_TOAST_SUCCESS_PAT)
        if ok.count() > 0:
            try:
                return True, ok.first.inner_text().strip()
            except Exception:
                return True, "(success toast, text unreadable)"

        page.wait_for_timeout(step)
        elapsed += step
    return None, ""


# ── Tab navigation ─────────────────────────────────────────────────────────────
def _go_to_tab(page, name: str):
    _dismiss_dialog(page)
    btn = page.get_by_role("button", name=name)
    btn.wait_for(state="visible", timeout=15_000)
    btn.first.click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1_500)


# ── Labeled field (Batch ID, Hall Ticket Prefix, Student Prefix) ───────────────
def _update_labeled_field(page, label_text: str, desired, field_name: str) -> str:
    if is_empty(desired):
        print(f"  {field_name} → SKIP (blank in CSV)")
        return SKIPPED

    desired = str(desired).strip()
    try:
        section = page.locator("div.p-3").filter(
            has=page.locator("span.text-gray-600", has_text=label_text)
        )
        section.wait_for(state="visible", timeout=6_000)
        pencil = section.locator("button.text-blue-600")
        pencil.wait_for(state="visible", timeout=6_000)
        pencil.click()
        page.wait_for_timeout(600)

        textbox = page.get_by_role("textbox").first
        textbox.wait_for(state="visible", timeout=6_000)
        current = textbox.input_value().strip()

        if current == desired:
            print(f"  {field_name} → SKIP (already '{desired}')")
            try:
                page.get_by_role("button", name="Cancel").click()
            except Exception:
                page.keyboard.press("Escape")
            page.wait_for_timeout(400)
            return SKIPPED

        print(f"  {field_name} → UPDATE '{current}' → '{desired}'")
        textbox.fill(desired)
        page.wait_for_timeout(200)
        page.get_by_role("button", name="Save Changes").click()
        page.wait_for_timeout(800)
        return CHANGED

    except Exception as e:
        print(f"  {field_name} → FAILED: {e}")
        try:
            page.get_by_role("button", name="Cancel").click()
        except Exception:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
        return FAILED


def _update_batch_id(page, desired) -> str:
    # Batch ID is auto-mapped from elsewhere (its edit pencil actually opens an
    # "Edit lmsBatchId" dialog, not the batch code) — never write to it directly.
    print("  Batch ID → SKIP (auto-mapped, not directly editable)")
    return SKIPPED

def _update_hall_ticket_prefix(page, desired) -> str:
    return _update_labeled_field(page, "Hall Ticket Prefix", desired, "Hall Ticket Prefix")

def _update_student_prefix(page, desired) -> str:
    return _update_labeled_field(page, "Student Prefix", desired, "Student Prefix")


# Batch Start Date moved from the Dates-tab table (now read-only there) to a
# pencil-edit card on Basic Details, same layout as Batch ID — but its field
# is a real datetime-local input, so it needs date parsing like _update_date_field.
def _update_batch_start_date(page, desired_csv) -> str:
    field_name = "Batch Start Date"
    if is_empty(desired_csv):
        print(f"  {field_name} → SKIP (blank in CSV)")
        return SKIPPED

    desired_dt = parse_dt(str(desired_csv).strip())
    if not desired_dt:
        print(f"  {field_name} → FAILED (cannot parse date '{desired_csv}')")
        return FAILED

    try:
        section = page.locator("div.p-3").filter(
            has=page.locator("span.text-gray-600", has_text=field_name)
        )
        section.wait_for(state="visible", timeout=6_000)
        pencil = section.locator("button.text-blue-600")
        pencil.wait_for(state="visible", timeout=6_000)
        pencil.click()
        page.wait_for_timeout(600)

        dt_input = page.get_by_role("textbox").first
        dt_input.wait_for(state="visible", timeout=6_000)
        current = dt_input.input_value().strip()

        if current == desired_dt:
            print(f"  {field_name} → SKIP (already '{dt_display(current)}')")
            try:
                page.get_by_role("button", name="Cancel").click()
            except Exception:
                page.keyboard.press("Escape")
            page.wait_for_timeout(400)
            return SKIPPED

        print(f"  {field_name} → UPDATE "
              f"'{dt_display(current) if current else 'empty'}' → '{dt_display(desired_dt)}'")
        dt_input.fill(desired_dt)
        page.wait_for_timeout(200)
        page.get_by_role("button", name="Save Changes").click()
        page.wait_for_timeout(800)
        return CHANGED

    except Exception as e:
        print(f"  {field_name} → FAILED: {e}")
        try:
            page.get_by_role("button", name="Cancel").click()
        except Exception:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
        return FAILED


# ── Date fields ────────────────────────────────────────────────────────────────
def _update_date_field(page, row_label: str, desired_csv, field_name: str) -> str:
    blank      = is_empty(desired_csv)
    desired_dt = None if blank else parse_dt(str(desired_csv).strip())

    if not blank and not desired_dt:
        print(f"  {field_name} → FAILED (cannot parse date '{desired_csv}')")
        return FAILED

    try:
        row = page.locator("tr").filter(
            has=page.locator("td", has_text=re.compile(rf"^{re.escape(row_label)}$"))
        )
        row.wait_for(state="visible", timeout=6_000)

        dt_input = row.locator("input[type='datetime-local']")
        dt_input.wait_for(state="visible", timeout=6_000)
        current = dt_input.input_value().strip()

        if blank:
            if not current:
                print(f"  {field_name} → SKIP (already empty)")
                return SKIPPED
            print(f"  {field_name} → CLEAR (was '{dt_display(current)}')")
            row.locator("button[aria-label='Clear date']").wait_for(state="visible", timeout=6_000)
            row.locator("button[aria-label='Clear date']").click()
            page.wait_for_timeout(800)
            return CHANGED

        if current == desired_dt:
            print(f"  {field_name} → SKIP (already '{dt_display(current)}')")
            return SKIPPED

        print(f"  {field_name} → UPDATE "
              f"'{dt_display(current) if current else 'empty'}' → '{dt_display(desired_dt)}'")
        dt_input.evaluate(
            f"el => {{ el.value = '{desired_dt}'; "
            f"el.dispatchEvent(new Event('input', {{bubbles: true}})); "
            f"el.dispatchEvent(new Event('change', {{bubbles: true}})); }}"
        )
        page.wait_for_timeout(800)
        return CHANGED

    except Exception as e:
        print(f"  {field_name} → FAILED: {e}")
        return FAILED


# ── LMS Settings ──────────────────────────────────────────────────────────────
# The LMS Settings card now has five near-identical section pickers stacked on
# the same page (Fallback, After Full Fee, Student, Working Professional, After
# Secure Seat). A page-wide "all chips" / "all Add-sections buttons" selector
# would touch the wrong bucket, so every bucket op is scoped to its own card via
# _bucket_container() before touching chips or the add-sections button.

def _bucket_container(page, heading_text: str, add_button_name: str):
    """Locate the specific section-bucket card by pairing its heading text with
    its own 'Add sections...' button, so operations don't leak into a sibling
    bucket that happens to share chip/button markup."""
    return page.locator("div").filter(
        has_text=re.compile(re.escape(heading_text))
    ).filter(
        has=page.get_by_role("button", name=add_button_name)
    ).last


def _update_section_bucket(page, heading_text: str, add_button_name: str,
                            search_placeholder: str, desired_csv, field_name: str) -> str:
    sections_raw = "" if is_empty(desired_csv) else str(desired_csv).strip()
    sections = [s.strip() for s in sections_raw.split(",") if s.strip()]
    if not sections:
        print(f"  {field_name} → SKIP (blank in CSV)")
        return SKIPPED

    try:
        container = _bucket_container(page, heading_text, add_button_name)
        container.wait_for(state="visible", timeout=6_000)

        def _chips():
            # Chip remove icons are icon-only buttons with NO accessible text
            # (confirmed via live recording) — not a literal "×" glyph.
            return container.get_by_role("button", name=re.compile(r"^$"))

        # Check what's actually there before touching anything — a prior batch-ID
        # change may or may not have reset this bucket, so don't assume either way.
        try:
            current_text = container.inner_text()
        except Exception:
            current_text = ""
        current_count = _chips().count()
        already_matches = (
            current_count == len(sections)
            and all(s.lower() in current_text.lower() for s in sections)
        )
        if already_matches:
            print(f"  {field_name} → SKIP (already set to {sections})")
            return SKIPPED
        print(f"    Currently: {current_count} section(s) selected — desired: {sections}")

        def _open_dropdown():
            container.get_by_role("button", name=add_button_name).first.click()
            page.wait_for_timeout(1_000)

        def _search_box():
            box = page.get_by_placeholder(search_placeholder)
            if box.count() == 0:
                box = page.locator("input[placeholder*='Search sections']")
            return box.first

        def _selected_count() -> int:
            done_btn = container.get_by_role("button").filter(
                has_text=re.compile(r"Done \(\d+ selected\)", re.I)
            )
            if done_btn.count() == 0:
                return -1  # sentinel: Done counter not found/visible
            try:
                txt = done_btn.first.inner_text()
            except Exception:
                return -1
            m = re.search(r"Done \((\d+) selected\)", txt)
            return int(m.group(1)) if m else -1

        def _row_is_selected(row) -> bool:
            for attr in ("aria-pressed", "aria-checked", "aria-selected"):
                try:
                    val = row.get_attribute(attr)
                except Exception:
                    val = None
                if val is not None:
                    return val.lower() == "true"
            return False

        # 1) Remove visible chip-pills (Fallback / After Full Fee / After Secure
        #    Seat show these outside the dropdown).
        removed = 0
        for _ in range(50):
            rm = _chips()
            if rm.count() == 0:
                break
            rm.first.click()
            page.wait_for_timeout(300)
            removed += 1
        if removed:
            print(f"    Cleared {removed} existing chip(s)")
        else:
            print("    No existing chips to clear")

        # 2) The Persona buckets (Student / Working Professional) don't render a
        #    chip-pill at all — proven live: the dropdown's own "Done (N selected)"
        #    counter showed 2 pre-existing selections even though 0 chips were
        #    visible outside it. Clearing the pills above isn't enough there, so
        #    open the picker, and un-toggle whatever it still reports as selected.
        _open_dropdown()
        search_box = _search_box()
        if search_box.count() > 0:
            try:
                search_box.fill("")
            except Exception:
                pass
            page.wait_for_timeout(600)

        stale = _selected_count()
        if stale > 0:
            print(f"    Dropdown reports {stale} pre-existing selection(s) with no visible chip — clearing via toggle")
            done_pat = re.compile(r"Done \(\d+ selected\)", re.I)
            for _ in range(stale + 5):
                if _selected_count() <= 0:
                    break
                rows = container.get_by_role("button")
                unclicked_one = False
                for i in range(rows.count()):
                    row = rows.nth(i)
                    try:
                        txt = row.inner_text().strip()
                    except Exception:
                        continue
                    if done_pat.search(txt) or txt == add_button_name.rstrip("."):
                        continue
                    if _row_is_selected(row):
                        before_stale = _selected_count()
                        row.click()
                        page.wait_for_timeout(500)
                        if _selected_count() < before_stale:
                            unclicked_one = True
                            break
                if not unclicked_one:
                    print("    [WARN] Could not identify remaining stale selection(s) via "
                          "aria-pressed/checked/selected — they may persist alongside new picks")
                    break
            remaining = _selected_count()
            if remaining > 0:
                print(f"    [WARN] {remaining} stale selection(s) still present after clearing attempt")
            else:
                print("    Stale selection(s) cleared")

        def _try_select(section: str) -> bool:
            if _search_box().count() == 0:
                _open_dropdown()
            search = _search_box()
            search.wait_for(state="visible", timeout=6_000)
            before = _chips().count()
            before_selected = _selected_count()
            search.fill(section)
            page.wait_for_timeout(1_200)

            pattern  = re.compile(re.escape(section), re.I)
            done_pat = re.compile(r"Done \(\d+ selected\)", re.I)

            def _best_candidate(locator):
                for i in range(locator.count()):
                    btn = locator.nth(i)
                    try:
                        txt = btn.inner_text().strip()
                    except Exception:
                        continue
                    if done_pat.search(txt):
                        continue
                    return btn, txt
                return None, None

            # Prefer a match scoped to this bucket's own card — with five
            # near-identical pickers on one page, an unscoped page-wide search
            # can click the wrong element and silently no-op (chip count won't
            # move). Only fall back to page-wide if the results render outside
            # this container (e.g. via a portal).
            chosen, chosen_txt = _best_candidate(container.get_by_role("button").filter(has_text=pattern))
            scope = "container"
            if chosen is None:
                chosen, chosen_txt = _best_candidate(page.get_by_role("button").filter(has_text=pattern))
                scope = "page-wide"

            if chosen is None:
                print(f"      no result found for '{section}'")
                return False

            print(f"      clicking ({scope}): '{chosen_txt}'")
            chosen.click()
            page.wait_for_timeout(1_200)

            # Re-check the Done-counter AFTER the click regardless of whether it
            # existed BEFORE — a bucket starting from 0 selections may not render
            # "Done (N selected)" until the first pick lands, so checking only
            # the pre-click state permanently fell back to the chip-count method
            # even for buckets (Student/Working Professional) that never show a
            # chip-pill at all, causing false failures on every attempt.
            after_selected = _selected_count()
            if after_selected >= 0:
                baseline = before_selected if before_selected >= 0 else 0
                if after_selected > baseline:
                    return True
                print(f"      click registered but selected-count unchanged "
                      f"({baseline} → {after_selected}) — retrying")
                return False

            after = _chips().count()
            if after <= before:
                print(f"      click registered but chip count unchanged ({before} → {after}) — retrying")
                return False
            return True

        if _search_box().count() == 0:
            _open_dropdown()
        ok, fail = [], []
        for i, section in enumerate(sections):
            print(f"    [{i+1}/{len(sections)}] '{section}'")
            succeeded = False
            for attempt in range(1, 4):
                try:
                    if _try_select(section):
                        print(f"      ✓ selected (attempt {attempt})")
                        ok.append(section)
                        succeeded = True
                        break
                    print(f"      attempt {attempt}: result not found, retrying…")
                    page.wait_for_timeout(800)
                except Exception as ex:
                    print(f"      attempt {attempt} error: {ex}")
                    page.wait_for_timeout(800)
            if not succeeded:
                print(f"      ✗ FAILED after 3 attempts")
                fail.append(section)

        print(f"    Summary: {len(ok)}/{len(sections)} selected — ok={ok} fail={fail}")

        done_btn = page.get_by_role("button").filter(has_text=re.compile(r"Done \(\d+ selected\)"))
        if done_btn.count() > 0:
            done_btn.first.click()
            page.wait_for_timeout(800)
        else:
            print("    [WARN] 'Done' button not found — changes may not be saved")

        return CHANGED if ok else FAILED

    except Exception as e:
        print(f"  {field_name} → FAILED: {e}")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return FAILED


def _update_trial_access_toggle(page, desired) -> str:
    """LMS Trial Access Before Full Fee — unlike the Kit Settings toggles, this
    does NOT auto-save on click; it only takes effect when 'Save LMS Settings'
    is clicked afterward, so the caller must ensure that save fires even if
    this is the only field that changed."""
    field_name = "LMS Trial Access Before Full Fee"
    desired_bool = to_bool(desired)
    if desired_bool is None:
        print(f"  {field_name} → SKIP (blank in CSV)")
        return SKIPPED
    try:
        row = page.locator("div").filter(
            has_text=re.compile(re.escape(field_name))
        ).filter(
            has=page.locator("input[type='checkbox']")
        ).last
        row.wait_for(state="visible", timeout=6_000)
        checkbox = row.locator("input[type='checkbox']")
        current  = checkbox.is_checked()
        if current == desired_bool:
            print(f"  {field_name} → SKIP (already {'ON' if desired_bool else 'OFF'})")
            return SKIPPED
        print(f"  {field_name} → UPDATE → {'ON' if desired_bool else 'OFF'} (takes effect on Save LMS Settings)")
        row.locator("[data-part='control']").click()
        page.wait_for_timeout(400)
        if checkbox.is_checked() != desired_bool:
            print(f"    [WARN] Toggle verify failed")
            return FAILED
        return CHANGED
    except Exception as e:
        print(f"  {field_name} → FAILED: {e}")
        return FAILED


# A batch that isn't used by any real cohort — selecting it, then selecting the
# real target batch, is a deliberate two-step reset: reselecting Batch ID (even
# to the same value) wipes every section bucket, and unlike trying to detect
# and un-toggle stale hidden selections per bucket (unreliable — some buckets
# don't expose a chip-pill for existing picks at all), forcing the wipe via a
# disposable batch swap guarantees every bucket starts genuinely empty.
THROWAWAY_BATCH = "IITMD-DM-EN-I-2608"


def _batch_trigger(page):
    # .last narrows to the innermost matching div/section (consistent with
    # _bucket_container's pattern) — without it, EVERY ancestor div up to the
    # page body also contains "LMS Batch" text somewhere in its subtree, so
    # .first picked the outermost/broadest match and grabbed an unrelated
    # button (confirmed live: it returned a "Counselling" button instead).
    return page.locator("div, section").filter(
        has=page.locator("text=/LMS Batch/i")
    ).last.get_by_role("button").first


def _select_batch(page, batch_value: str) -> bool:
    """Search+select a batch in the LMS Batch ID dropdown. Returns True if the
    selection appears to have taken.

    Verifies via the numeric #<id> rather than the searched name/text: the
    closed-state trigger only ever renders a generic "#<id> Batch #<id>" label
    (confirmed live), never the friendly name, so checking for the searched
    text on the page afterward is unreliable and previously false-negatived a
    click that actually worked, aborting the whole reset dance early."""
    try:
        try:
            page.locator(".lms-batch-dropdown button").first.click()
        except Exception:
            _batch_trigger(page).click()
        page.wait_for_timeout(800)

        search = page.get_by_placeholder("Search batches...")
        search.wait_for(state="visible", timeout=8_000)
        search.fill(batch_value)
        page.wait_for_timeout(1_200)

        candidate = page.get_by_role("button").filter(
            has_text=re.compile(re.escape(batch_value), re.I)
        ).first
        candidate.wait_for(state="visible", timeout=6_000)
        try:
            expected_id_match = re.search(r"#(\d+)", candidate.inner_text())
        except Exception:
            expected_id_match = None
        expected_id = expected_id_match.group(1) if expected_id_match else None

        candidate.click()
        page.wait_for_timeout(800)

        if expected_id:
            try:
                trigger_text = _batch_trigger(page).inner_text()
            except Exception:
                trigger_text = ""
            m = re.search(r"#(\d+)", trigger_text)
            if m and m.group(1) == expected_id:
                return True
            print(f"    [WARN] batch verify: expected #{expected_id}, trigger shows '{trigger_text.strip()}'")
            return False

        # Couldn't extract an ID from the candidate — fall back to text presence.
        return page.locator(f"text={batch_value}").count() > 0
    except Exception as e:
        print(f"    [ERROR] batch select '{batch_value}': {e}")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False


def _update_lms_settings(page, row) -> dict:
    results = {
        "lms_batch_id": SKIPPED,
        "lms_section_ids": SKIPPED,
        "lms_section_ids_after_full_fee": SKIPPED,
        "lms_section_ids_student": SKIPPED,
        "lms_section_ids_working_professional": SKIPPED,
        "lms_trial_access": SKIPPED,
        "lms_section_ids_after_secure_seat": SKIPPED,
        "manager_id": SKIPPED,
    }

    lms_batch  = str(row.get("lms_batch_id", "")).strip() if not is_empty(row.get("lms_batch_id")) else ""
    manager_id = str(row.get("manager_id", "")).strip() if not is_empty(row.get("manager_id")) else ""

    section_bucket_cols = (
        "lms_section_ids", "lms_section_ids_after_full_fee",
        "lms_section_ids_student", "lms_section_ids_working_professional",
        "lms_trial_access", "lms_section_ids_after_secure_seat",
    )
    needs_section_work = any(not is_empty(row.get(c)) for c in section_bucket_cols)

    # The site itself requires Batch ID, at least one Fallback Section ID, and
    # Manager ID for LMS settings to save at all. The reset dance below wipes
    # the Fallback bucket to empty regardless of what's requested — if this row
    # doesn't also supply lms_section_ids, Save will very likely fail required-
    # field validation.
    fallback_blank = is_empty(row.get("lms_section_ids"))
    if (lms_batch or needs_section_work) and fallback_blank:
        print("  [WARN] lms_section_ids (Fallback) is blank in this row, but it's a "
              "REQUIRED field and the reset dance below will wipe it to empty — "
              "Save LMS Settings will likely fail validation unless it's filled in.")

    if lms_batch or needs_section_work:
        target_batch = lms_batch
        if not target_batch:
            # No batch change requested, but sections still need a guaranteed-clean
            # slate — figure out the current batch so we can reselect it after
            # the throwaway reset (the closed trigger only shows the numeric ID,
            # e.g. "#124 Batch #124", never a friendly name).
            try:
                m = re.search(r"#(\d+)", _batch_trigger(page).inner_text())
                if m:
                    target_batch = m.group(1)
            except Exception:
                pass

        if not target_batch:
            print("  LMS Batch ID → [WARN] could not determine current batch to "
                  "restore after reset — skipping section reset dance")
        else:
            # Do both selections back-to-back unconditionally — verification of
            # the intermediate throwaway step is informational only, never a
            # gate. A prior version aborted here whenever verification looked
            # shaky, leaving the cohort stuck on the throwaway batch without
            # ever attempting the real target — exactly the bug being fixed.
            print(f"  Resetting section config via throwaway batch swap "
                  f"('{THROWAWAY_BATCH}' → '{target_batch}')")
            throwaway_ok = _select_batch(page, THROWAWAY_BATCH)
            print(f"    throwaway batch selected: {throwaway_ok}")
            page.wait_for_timeout(500)
            target_ok = _select_batch(page, target_batch)
            if target_ok:
                print(f"    ✓ batch set to '{target_batch}' — sections reset to empty")
            else:
                print(f"    [WARN] could not confirm batch is now '{target_batch}' "
                      f"— proceeding anyway; section searches below will reveal if wrong")
            if lms_batch:
                results["lms_batch_id"] = CHANGED if target_ok else FAILED

    print("  LMS Section IDs (Fallback)")
    results["lms_section_ids"] = _update_section_bucket(
        page, "LMS Section IDs (Fallback)", "Add sections...", "Search sections...",
        row.get("lms_section_ids"), "LMS Section IDs (Fallback)"
    )

    print("  After Full Fee Section IDs")
    results["lms_section_ids_after_full_fee"] = _update_section_bucket(
        page, "After Full Fee Section IDs", "Add sections for After Full Fee...",
        "Search sections for After Full Fee...",
        row.get("lms_section_ids_after_full_fee"), "After Full Fee Section IDs"
    )

    print("  Persona: Student Section IDs")
    results["lms_section_ids_student"] = _update_section_bucket(
        page, "Student", "Add sections for Student...", "Search sections for Student...",
        row.get("lms_section_ids_student"), "Student Section IDs"
    )

    print("  Persona: Working Professional Section IDs")
    results["lms_section_ids_working_professional"] = _update_section_bucket(
        page, "Working Professional", "Add sections for Working Professional...",
        "Search sections for Working Professional...",
        row.get("lms_section_ids_working_professional"), "Working Professional Section IDs"
    )

    print("  LMS Trial Access Before Full Fee")
    results["lms_trial_access"] = _update_trial_access_toggle(page, row.get("lms_trial_access"))

    trial_desired = to_bool(row.get("lms_trial_access"))
    after_secure_seat_csv = row.get("lms_section_ids_after_secure_seat")
    if not is_empty(after_secure_seat_csv) and trial_desired is False:
        print("  After Secure Seat Section IDs → SKIP (lms_trial_access is FALSE — field is hidden)")
        results["lms_section_ids_after_secure_seat"] = SKIPPED
    else:
        print("  After Secure Seat Section IDs")
        results["lms_section_ids_after_secure_seat"] = _update_section_bucket(
            page, "After Secure Seat Section IDs", "Add sections for After Secure Seat...",
            "Search sections for After Secure Seat...",
            after_secure_seat_csv, "After Secure Seat Section IDs"
        )

    if manager_id:
        print(f"  Manager ID → '{manager_id}'")
        try:
            mgr = page.get_by_placeholder("Enter manager ID")
            mgr.wait_for(state="visible", timeout=6_000)
            current = mgr.input_value().strip()
            if current == manager_id:
                print(f"    SKIP (already '{manager_id}')")
            else:
                mgr.fill(manager_id)
                mgr.press("Tab")
                page.wait_for_timeout(400)
                results["manager_id"] = CHANGED
        except Exception as e:
            print(f"    [ERROR] Manager ID: {e}")
            results["manager_id"] = FAILED

    # Save — only if the UI actually registered a change (save button appears)
    lms_attempted = any(results[k] == CHANGED for k in (
        "lms_batch_id", "lms_section_ids", "lms_section_ids_after_full_fee",
        "lms_section_ids_student", "lms_section_ids_working_professional",
        "lms_trial_access", "lms_section_ids_after_secure_seat", "manager_id",
    ))
    if lms_attempted:
        save_btn = page.locator("button").filter(has_text=re.compile(r"Save LMS", re.I))
        lms_fields = (
            "lms_batch_id", "lms_section_ids", "lms_section_ids_after_full_fee",
            "lms_section_ids_student", "lms_section_ids_working_professional",
            "lms_trial_access", "lms_section_ids_after_secure_seat", "manager_id",
        )
        try:
            # Let the page settle after the last bucket's "Done" click before
            # looking for Save — confirmed live that 3s wasn't always enough,
            # causing Save to never get clicked at all (no toast, no 5s pause,
            # and the real changes silently mislabeled as SKIPPED below).
            page.wait_for_timeout(1_000)
            save_btn.wait_for(state="visible", timeout=8_000)
            save_btn.first.click()
            toast_ok, toast_text = _wait_for_toast(page)
            if toast_ok is False:
                print(f"  [LMS SAVE FAILED] notification: '{toast_text}'")
                for k in lms_fields:
                    if results[k] == CHANGED:
                        results[k] = FAILED
                results["notes"] = (results.get("notes", "") + f" | LMS save error toast: {toast_text}").strip(" |")
            elif toast_ok is True:
                print(f"  [LMS SAVED] notification: '{toast_text}'")
            elif fallback_blank:
                print("  [LMS SAVE UNCERTAIN] no confirmation toast observed, and "
                      "lms_section_ids (Fallback, required) was left blank by this "
                      "row's reset — treating as FAILED rather than assuming success.")
                for k in lms_fields:
                    if results[k] == CHANGED:
                        results[k] = FAILED
                results["notes"] = (results.get("notes", "") +
                                     " | LMS save uncertain: required Fallback section left blank, no confirming toast").strip(" |")
            else:
                print("  [LMS SAVED] (no confirmation toast observed within timeout — treating click as success)")

            # Deliberate pause so the on-screen notification (top-right) stays
            # visible long enough to read/confirm before moving on to the next
            # step or cohort, or closing the browser.
            page.wait_for_timeout(5_000)
        except Exception as e:
            # We only get here when lms_attempted is True, i.e. something in
            # this bucket run was genuinely marked CHANGED — so a missing Save
            # button means the change never got persisted, not that nothing
            # needed saving. Treating it as SKIPPED (the old assumption) would
            # silently misreport a real failure as if everything were fine.
            print(f"  [LMS SAVE FAILED] Save button never appeared/clickable: {e}")
            for k in lms_fields:
                if results[k] == CHANGED:
                    results[k] = FAILED
            results["notes"] = (results.get("notes", "") +
                                 " | LMS save failed: Save LMS Settings button not found/clickable").strip(" |")

    return results


# ── Kit toggles ────────────────────────────────────────────────────────────────
def _update_toggle(page, label_contains: str, desired, field_name: str) -> str:
    desired_bool = to_bool(desired)
    if desired_bool is None:
        print(f"  {field_name} → SKIP (blank in CSV)")
        return SKIPPED
    try:
        row = page.locator("div.p-3").filter(
            has=page.locator("span.text-gray-600", has_text=label_contains)
        )
        row.wait_for(state="visible", timeout=6_000)
        checkbox = row.locator("input[type='checkbox']")
        current  = checkbox.is_checked()
        if current == desired_bool:
            print(f"  {field_name} → SKIP (already {'ON' if desired_bool else 'OFF'})")
            return SKIPPED
        print(f"  {field_name} → UPDATE → {'ON' if desired_bool else 'OFF'}")
        row.locator("[data-part='control']").click()
        page.wait_for_timeout(600)
        if checkbox.is_checked() != desired_bool:
            print(f"    [WARN] Toggle verify failed")
            return FAILED
        return CHANGED
    except Exception as e:
        print(f"  {field_name} → FAILED: {e}")
        return FAILED


# ── Per-cohort processor ───────────────────────────────────────────────────────
def process_cohort(page, row, base_url: str = BASE_URL) -> dict:
    cohort_id = str(row["cohort_id"]).strip()
    s = {k: SKIPPED for k in RESULT_FIELDS}
    s["cohort_id"] = cohort_id
    s["notes"]     = ""

    print(f"  Loading: {base_url}/{cohort_id}")
    page.goto(f"{base_url}/{cohort_id}")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(3_000)
    _dismiss_dialog(page)

    print("  [Basic Details]")
    _go_to_tab(page, "Basic Details")
    s["batch_id"]         = _update_batch_id(page, row.get("batch_id"))
    s["batch_start_date"] = _update_batch_start_date(page, row.get("batch_start_date"))

    print("  [Identifiers]")
    _go_to_tab(page, "Identifiers")
    s["hall_ticket_prefix"] = _update_hall_ticket_prefix(page, row.get("hall_ticket_prefix"))
    s["student_prefix"]     = _update_student_prefix(page, row.get("student_prefix"))

    print("  [Dates]")
    _go_to_tab(page, "Dates")
    s["foundation_starts"] = _update_date_field(
        page, "Foundation Starts", row.get("foundation_starts"), "Foundation Starts"
    )

    print("  [Course Onboarding]")
    _go_to_tab(page, "Course Onboarding")
    s.update(_update_lms_settings(page, row))
    s["enable_kit"] = _update_toggle(page, "Enable Kit", row.get("enable_kit"), "Enable Kit")
    s["disable_welcome_kit_tshirt"] = _update_toggle(
        page, "Disable Welcome Kit T-Shirt",
        row.get("disable_welcome_kit_tshirt"), "Disable Welcome Kit T-Shirt"
    )
    return s


# ── Shared internals ───────────────────────────────────────────────────────────
def _launch_context(p, profile_dir: str):
    return p.chromium.launch_persistent_context(
        user_data_dir=profile_dir,
        headless=False,
        slow_mo=300,
        args=["--start-maximized"],
        no_viewport=True,
    )


def _run_update_loop(page, df: pd.DataFrame, base_url: str) -> list:
    all_results = []
    total = len(df)
    for i, row in df.iterrows():
        cohort_id = str(row.get("cohort_id", "")).strip()
        print(f"{'─'*60}")
        print(f"[{i+1}/{total}] Cohort ID: {cohort_id}")
        try:
            result = process_cohort(page, row, base_url=base_url)
        except Exception as e:
            print(f"  [ERROR] {e}")
            result = {k: ERROR for k in RESULT_FIELDS}
            result["cohort_id"] = cohort_id
            result["notes"]     = str(e)
        all_results.append(result)
        print()
    return all_results


def _write_report(all_results: list, log_stem: str, src_csv: str) -> str:
    csv_out = os.path.join(LOGS_DIR, f"{log_stem}.csv")
    pd.DataFrame(all_results).to_csv(csv_out, index=False)
    print(f"\nCSV report  → {csv_out}")

    archive_name = log_stem[len("run_"):]
    dest = os.path.join(ARCHIVE_DIR, f"{archive_name}.csv")
    shutil.copy2(src_csv, dest)
    print(f"Input archived → {dest}")

    df_log = pd.DataFrame(all_results)
    print("\n══ Summary ══════════════════════════════════════════════")
    for col in SUMMARY_FIELDS:
        if col in df_log.columns:
            print(f"  {col:35s}: {df_log[col].value_counts().to_dict()}")

    skip_keys = {"cohort_id", "notes"}
    failed = [s for s in all_results
              if any(v in (FAILED, ERROR) for k, v in s.items() if k not in skip_keys)]
    print(f"\n  Cohorts with failures/errors: {len(failed)}/{len(all_results)}")

    if failed:
        print("\n  ── Failed / Error cohort IDs ─────────────────────────")
        for s in failed:
            cid  = s.get("cohort_id", "?")
            bad  = {k: v for k, v in s.items() if k not in skip_keys and v in (FAILED, ERROR)}
            note = s.get("notes", "")
            line = f"    [{cid}]  {bad}"
            if note:
                line += f"  — {note}"
            print(line)
        print("  ─────────────────────────────────────────────────────")

    print("═════════════════════════════════════════════════════════")
    print("Done.")
    return csv_out


def _apply_start_cohort(df: pd.DataFrame, start_cohort: str) -> pd.DataFrame | None:
    ids  = df["cohort_id"].astype(str).str.strip()
    mask = ids == str(start_cohort).strip()
    if not mask.any():
        print(f"[ERROR] cohort_id '{start_cohort}' not found in CSV.")
        return None
    df = df[mask.cumsum() >= 1].reset_index(drop=True)
    print(f"Resuming from cohort {start_cohort} — {len(df)} row(s) remaining")
    return df


# ── Login helper ───────────────────────────────────────────────────────────────
def _ensure_logged_in(login_url: str, profile_dir: str):
    print("\n── Step 1 of 2: Login check ─────────────────────────────")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=False,
            args=["--start-maximized"],
            no_viewport=True,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(login_url)
        page.wait_for_load_state("networkidle")

        if (login_url.rstrip("/") in page.url.rstrip("/")
                or "login" in page.url.lower()
                or "signup" in page.url.lower()):
            print("Session expired — please log in with OTP in the browser window.")
            input("Press ENTER once you are on the dashboard... ")
            page.wait_for_load_state("networkidle", timeout=60_000)
            print(f"Logged in. URL: {page.url}")
        else:
            print(f"Session active. URL: {page.url}")

        input("Press ENTER to start updating cohorts... ")
        context.close()
    print("Login confirmed. Opening browser for updates...\n")


# ── Entry point ────────────────────────────────────────────────────────────────
def run(base_url: str, login_url: str, profile_dir: str, start_cohort: str = ""):
    csv_files = sorted(glob.glob(os.path.join(INPUT_DIR, "*.csv")))

    if not csv_files:
        print(f"[ERROR] No CSV files found in {INPUT_DIR}/")
        print("Place your input CSV in the input/ folder and re-run.")
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

    df = pd.read_csv(chosen, dtype=str)
    if "cohort_id" not in df.columns:
        print("[ERROR] CSV must have a 'cohort_id' column.")
        return

    if start_cohort:
        df = _apply_start_cohort(df, start_cohort)
        if df is None:
            return

    print(f"\nRows to process: {len(df)}")

    _ensure_logged_in(login_url=login_url, profile_dir=profile_dir)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base      = os.path.splitext(os.path.basename(chosen))[0]
    log_stem  = f"run_{base}_{timestamp}"

    _start_log(log_stem)
    print("── Step 2 of 2: Cohort updates ──────────────────────────")
    print("Starting cohort updates...\n")

    with sync_playwright() as p:
        context     = _launch_context(p, profile_dir)
        page        = context.pages[0] if context.pages else context.new_page()
        all_results = _run_update_loop(page, df, base_url)
        context.close()

    _write_report(all_results, log_stem, chosen)
    _stop_log()


# ── CLI ────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Masai cohort management updater")
    parser.add_argument("--platform",     choices=["masai", "prepleaf"], default=DEFAULT_PLATFORM)
    parser.add_argument("--base-url",     default=None)
    parser.add_argument("--login-url",    default=None)
    parser.add_argument("--profile-dir",  default=None)
    parser.add_argument("--start-cohort", default="", metavar="COHORT_ID",
                        help="Resume from this cohort_id")
    args = parser.parse_args()

    defaults    = PLATFORMS[args.platform]
    base_url    = args.base_url    or defaults["base_url"]
    login_url   = args.login_url   or defaults["login_url"]
    profile_dir = args.profile_dir or defaults["profile_dir"]

    run(base_url=base_url, login_url=login_url, profile_dir=profile_dir,
        start_cohort=args.start_cohort)
