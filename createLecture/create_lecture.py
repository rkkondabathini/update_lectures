"""
create_lecture.py — Bulk lecture creator
Site: https://experience-admin.masaischool.com/lectures/create/

Required CSV columns:
  title, batch, section, category, tags, type,
  schedule_date, schedule_time, concludes_date, concludes_time,
  host_email, zoom_link, module, show_feedback,
  + EITHER 'mandatory' (TRUE/FALSE) OR 'optional' (yes/no — inverse)

Place TWO CSVs in ./input/:
  1. Data CSV (must have a 'title' column)
  2. Hosts CSV with 'Name' and 'Email' columns
     (used to translate host_email -> host_name for the search field)

Run:
  python create_lecture.py
"""

import re
import os
import sys
import time
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
LOGIN_URL  = "https://experience-admin.masaischool.com/"
CREATE_URL = "https://experience-admin.masaischool.com/lectures/create/"
LIST_URL   = "https://experience-admin.masaischool.com/lectures/?page=0"
EMAIL      = "ravi.kiran@masaischool.com"
PASSWORD   = "mAs@!4321"

# ── Status constants ──────────────────────────────────────────────────────────
SKIPPED = "SKIPPED"
CREATED = "CREATED"
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


_tee: _Tee | None = None


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


# ═════════════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════════════

def to_bool(val) -> bool:
    return str(val).strip().upper() in ("TRUE", "YES", "1")


def is_blank(val) -> bool:
    if val is None:
        return True
    try:
        if pd.isna(val):
            return True
    except Exception:
        pass
    return str(val).strip() == ""


def combine_dt(date_val, time_val) -> str:
    """Combine date + time strings into YYYY-MM-DDTHH:MM (datetime-local format)."""
    date_str = str(date_val).strip()
    time_str = str(time_val).strip()
    # Normalise time to HH:MM
    if len(time_str) >= 5:
        time_str = time_str[:5]
    # Normalise date to YYYY-MM-DD if needed
    try:
        d = pd.to_datetime(date_str, dayfirst=False).strftime("%Y-%m-%d")
    except Exception:
        d = date_str
    return f"{d}T{time_str}"


def _wait_for_form(page):
    try:
        page.wait_for_selector('button:has-text("Create")', state="visible", timeout=15_000)
    except Exception:
        pass
    page.wait_for_timeout(500)


# ── React-select dropdown helpers (reused from update_lecture pattern) ───────

def _click_dropdown_input(page, label: str):
    """Click the react-select input adjacent to a label matching `label`."""
    clicked = page.evaluate("""(labelText) => {
        const labels = [...document.querySelectorAll('label')];
        const lbl = labels.find(l =>
            l.textContent.trim().toLowerCase().includes(labelText.toLowerCase())
        );
        if (!lbl) return false;
        for (const el of [lbl, lbl.parentElement, lbl.parentElement && lbl.parentElement.parentElement]) {
            if (!el) continue;
            const ic = el.querySelector('.react-select__input-container');
            if (ic) { ic.click(); return true; }
        }
        return false;
    }""", label)
    if not clicked:
        raise Exception(f"react-select not found near label '{label}'")


def _wait_for_options(page, timeout_ms: int = 5000) -> bool:
    """Poll until react-select options become visible, up to timeout."""
    elapsed = 0
    step = 250
    while elapsed < timeout_ms:
        try:
            opts = page.locator(".react-select__option")
            if opts.count() > 0 and opts.first.is_visible(timeout=200):
                return True
        except Exception:
            pass
        page.wait_for_timeout(step)
        elapsed += step
    return False


def _select_dropdown_option(page, label: str, value: str, wait_after_type_ms: int = 700) -> str:
    """Open react-select near `label`, type `value`, click matching option."""
    if is_blank(value):
        return SKIPPED
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        _click_dropdown_input(page, label)
        page.wait_for_timeout(500)
        page.keyboard.type(str(value), delay=50)
        page.wait_for_timeout(wait_after_type_ms)

        # If options not visible yet, poll for them
        if not _wait_for_options(page, timeout_ms=5000):
            page.keyboard.press("Escape")
            print(f"     [WARN] Dropdown '{label}': no options appeared for '{value}'")
            return FAILED

        desired_lower = str(value).strip().lower()
        options = page.locator(".react-select__option")

        # Try exact match first
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
            return CREATED

        page.keyboard.press("Escape")
        print(f"     [WARN] Dropdown '{label}': no option found for '{value}'")
        return FAILED
    except Exception as e:
        page.keyboard.press("Escape")
        print(f"     [WARN] Dropdown '{label}' failed: {e}")
        return FAILED


def _select_host_by_email(page, host_name: str, host_email: str) -> str:
    """Search the Primary host dropdown using multiple fallback queries; click
    the option whose text contains the matching email. Tries the configured
    name first, then the email's local-part, then just the first word of it."""
    if is_blank(host_email):
        return FAILED

    email_lower = host_email.strip().lower()
    local_part  = email_lower.split("@")[0]
    first_word  = local_part.split(".")[0]

    # Build the ordered list of search queries (skip duplicates / blanks).
    queries = []
    for q in (host_name, local_part, first_word):
        if q and not is_blank(q):
            q = str(q).strip()
            if q.lower() not in [x.lower() for x in queries]:
                queries.append(q)

    def _try(query: str):
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(250)
            _click_dropdown_input(page, "Primary host")
            page.wait_for_timeout(500)
            # Clear any text already typed (from a prior failed attempt)
            page.keyboard.press("Control+a")
            page.keyboard.press("Backspace")
            page.keyboard.type(query, delay=50)
            page.wait_for_timeout(5_000)
            if not _wait_for_options(page, timeout_ms=5_000):
                return None
            options = page.locator(".react-select__option")
            for i in range(options.count()):
                opt = options.nth(i)
                try:
                    if opt.is_visible(timeout=300):
                        text = opt.inner_text().strip().lower()
                        if email_lower in text:
                            return opt
                except Exception:
                    continue
            return None
        except Exception as e:
            print(f"     [WARN] Host search '{query}' errored: {e}")
            return None

    for query in queries:
        chosen = _try(query)
        if chosen is not None:
            try:
                chosen.click()
                page.wait_for_timeout(400)
                print(f"     [OK] Host found via search '{query}'")
                return CREATED
            except Exception as e:
                print(f"     [WARN] Host click failed after match on '{query}': {e}")
                continue

    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    print(f"     [WARN] Host '{host_name}' ({host_email}): no match after queries={queries}")
    return FAILED


def _read_datetime(page, label_pattern: str) -> str:
    """Read current value of the datetime-local input owned by the matching label."""
    try:
        idx = _datetime_input_index(page, label_pattern)
        if idx < 0:
            return ""
        return page.locator("input[type='datetime-local']").nth(idx).input_value().strip()
    except Exception:
        return ""


def _add_multi_tag(page, tag: str) -> bool:
    """Add one tag to the tags multi-select. Returns True if added."""
    try:
        input_container = page.locator(
            ".react-select__value-container--is-multi > .react-select__input-container"
        ).first
        input_container.click()
        page.wait_for_timeout(200)
        page.keyboard.type(tag, delay=50)
        page.wait_for_timeout(600)

        # Poll for options to appear (safety net for slower loads)
        if not _wait_for_options(page, timeout_ms=3_000):
            print(f"     [WARN] Tag '{tag}': no options appeared")
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
            return False

        option = page.locator(".react-select__option").first
        option.click()
        page.wait_for_timeout(400)
        return True
    except Exception as e:
        print(f"     [WARN] Tag '{tag}' failed: {e}")
        return False


# ── Text input helpers ───────────────────────────────────────────────────────

def _fill_input_by_label(page, label: str, value: str) -> str:
    """Fill a text/url input near a label matching `label`."""
    if is_blank(value):
        return SKIPPED
    try:
        # Try by placeholder first
        candidates = [
            page.get_by_placeholder(re.compile(label, re.I)),
            page.locator("label").filter(has_text=re.compile(label, re.I)).locator("input").first,
            page.locator("label").filter(has_text=re.compile(label, re.I)).locator("xpath=following::input[1]").first,
        ]
        for c in candidates:
            try:
                if c.count() > 0 and c.first.is_visible(timeout=500):
                    c.first.fill(str(value))
                    page.wait_for_timeout(200)
                    return CREATED
            except Exception:
                continue
        return FAILED
    except Exception as e:
        print(f"     [WARN] Input '{label}' failed: {e}")
        return FAILED


def _datetime_input_index(page, label_pattern: str) -> int:
    """Return the index (among all datetime-local inputs) of the input whose
    immediately preceding label in document order matches `label_pattern`.
    Returns -1 if no match.
    """
    return page.evaluate("""(pattern) => {
        const re = new RegExp(pattern, 'i');
        const inputs = [...document.querySelectorAll('input[type="datetime-local"]')];
        const allElems = [...document.querySelectorAll('*')];

        for (let i = 0; i < inputs.length; i++) {
            const inputIdx = allElems.indexOf(inputs[i]);
            // Walk back through document order looking for the closest preceding label
            for (let j = inputIdx - 1; j >= 0; j--) {
                if (allElems[j].tagName === 'LABEL') {
                    if (re.test(allElems[j].textContent.trim())) {
                        return i;
                    }
                    break;  // closest label doesn't match — this input belongs to another field
                }
            }
        }
        return -1;
    }""", label_pattern)


def _set_datetime(page, label_pattern: str, dt_value: str) -> str:
    """Set the datetime-local input owned by the label matching `label_pattern`.
    Uses the native HTMLInputElement value setter to properly notify React's
    internal value tracker — without this, React resets the value on the next
    re-render (which is why dates kept disappearing when other fields changed)."""
    if is_blank(dt_value):
        return SKIPPED
    try:
        idx = _datetime_input_index(page, label_pattern)
        if idx < 0:
            print(f"     [WARN] Datetime '{label_pattern}': no matching input found")
            return FAILED

        ok = page.evaluate("""(args) => {
            const inputs = [...document.querySelectorAll('input[type="datetime-local"]')];
            const el = inputs[args.idx];
            if (!el) return false;
            // Use prototype setter to bypass React's value tracker.
            const nativeSetter = Object.getOwnPropertyDescriptor(
                HTMLInputElement.prototype, 'value'
            ).set;
            nativeSetter.call(el, args.val);
            el.dispatchEvent(new Event('input',  {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
            // Also blur to ensure form validation runs
            el.dispatchEvent(new Event('blur', {bubbles: true}));
            return true;
        }""", {"idx": idx, "val": dt_value})

        if not ok:
            print(f"     [WARN] Datetime '{label_pattern}': set failed")
            return FAILED

        page.wait_for_timeout(400)
        print(f"     [OK] datetime[{idx}] '{label_pattern}' → '{dt_value}'")
        return CREATED
    except Exception as e:
        print(f"     [WARN] Datetime '{label_pattern}' failed: {e}")
        return FAILED


# ── Schedule defaults (Test Group / topic_001 / test_LO_001) ────────────────
# These must be set for the "Create" button to become enabled.

def _clear_and_select(page, label_locator, search_text: str, exact_text: str):
    removes = label_locator.locator(".react-select__multi-value__remove")
    while removes.count() > 0:
        removes.first.click()
        page.wait_for_timeout(150)
    clear_btn = label_locator.locator(".react-select__clear-indicator")
    if clear_btn.count() > 0:
        clear_btn.click()
        page.wait_for_timeout(150)
    label_locator.locator(".react-select__input-container").click()
    page.wait_for_timeout(200)
    page.keyboard.type(search_text, delay=50)
    page.wait_for_timeout(400)
    page.get_by_text(exact_text, exact=True).click()
    page.wait_for_timeout(200)


def _set_schedule_defaults(page) -> str:
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)
        page.wait_for_selector("div:nth-child(3) > .p-4 > .grid", state="visible", timeout=10_000)
        grid = page.locator("div:nth-child(3) > .p-4 > .grid")
        _clear_and_select(page, grid.locator("label").nth(0), "test group",  "Test Group")
        _clear_and_select(page, grid.locator("label").nth(1), "topic_001",   "topic_001")
        _clear_and_select(page, grid.locator("label").nth(2), "test_LO_001", "test_LO_001")
        return CREATED
    except Exception as e:
        print(f"  [WARN] Schedule defaults failed: {e}")
        return FAILED


# ── Toggles ──────────────────────────────────────────────────────────────────

def _set_toggle(page, label_pattern: str, desired: bool, field_name: str) -> str:
    """Set a Tailwind toggle switch (.w-11) to desired state."""
    try:
        label = page.locator("label").filter(has_text=re.compile(label_pattern, re.I))
        cb = label.locator("input[type='checkbox']").first
        if cb.count() == 0:
            print(f"     [WARN] {field_name}: checkbox not found near label")
            return FAILED

        current = cb.is_checked()
        if current == desired:
            return SKIPPED

        toggle = label.locator(".w-11").first
        if toggle.count() > 0:
            toggle.click()
        else:
            cb.click(force=True)
        page.wait_for_timeout(400)
        return CREATED
    except Exception as e:
        print(f"     [WARN] {field_name} toggle failed: {e}")
        return FAILED


# ── Payout type — only required for SOME hosts ───────────────────────────────
# When a host is selected and that host has Payout type required, a "Payout
# type *" dropdown appears in the host card with the validation message
# "Select a payout type to continue.". If we don't pick a value the Create
# button stays disabled. This helper picks the first valid option whenever
# the field is present; it's a no-op if the field doesn't appear.

def _select_payout_type_if_required(page, prefer: str = "live session", wait_seconds: int = 8) -> str:
    """If a 'Payout type *' field appears, pick the option matching `prefer`
    (default 'live session'); falls back to the first available option. No-op
    if the field isn't present.

    DOM shape (confirmed via Playwright codegen):
      <label class="...">
        Payout type *
        ...
        <div class="react-select">
          <div class="react-select__control">
            <div class="react-select__value-container">
              <div class="react-select__input-container">…</div>

    Strategy: find the <label> whose text starts with "Payout type" AND that
    contains a .react-select__input-container, click that input container to
    open the menu, then click the option matching `prefer` (or the first one).

    Called twice in the per-lecture flow:
      1. Just after the Primary Host is selected (catches in-card variant)
      2. Just before clicking Create (catches the 'PAYOUT PLAN (EXTERNAL HOSTS)'
         panel, which appears later for external hosts)
    """
    # Wait up to wait_seconds for the field to appear (it renders after host
    # selection for some hosts, and may take a moment).
    deadline = time.time() + wait_seconds
    label = None
    while time.time() < deadline:
        try:
            cand = (page.locator("label")
                        .filter(has_text=re.compile(r"Payout type", re.I))
                        .filter(has=page.locator(".react-select__input-container")))
            n = cand.count()
            for i in range(n):
                el = cand.nth(i)
                try:
                    if el.is_visible(timeout=200):
                        label = el
                        break
                except Exception:
                    continue
            if label is not None:
                break
        except Exception:
            pass
        page.wait_for_timeout(300)

    if label is None:
        return SKIPPED

    try:
        trigger = label.locator(".react-select__input-container").first
        trigger.click()
        page.wait_for_timeout(500)

        # Pick preferred option, else fall back to first visible option
        option = None
        try:
            cand = (page.locator(".react-select__option")
                        .filter(has_text=re.compile(re.escape(prefer), re.I)))
            if cand.count() > 0 and cand.first.is_visible(timeout=600):
                option = cand.first
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
            print(f"     [WARN] Payout type: dropdown opened but no options visible")
            try: page.keyboard.press("Escape")
            except Exception: pass
            return FAILED

        option.click()
        page.wait_for_timeout(400)
        print(f"     Payout type → '{prefer}'")
        return CREATED
    except Exception as e:
        print(f"     [WARN] Payout type click failed: {e}")
        try: page.keyboard.press("Escape")
        except Exception: pass
        return FAILED


# ═════════════════════════════════════════════════════════════════════════════
# Per-lecture create
# ═════════════════════════════════════════════════════════════════════════════

def create_lecture(page, row, hosts_map: dict) -> dict:
    s = {
        "title":         row.get("title", ""),
        "batch":         SKIPPED,
        "section":       SKIPPED,
        "category":      SKIPPED,
        "module":        SKIPPED,
        "type":          SKIPPED,
        "schedule":      SKIPPED,
        "concludes":     SKIPPED,
        "host":          SKIPPED,
        "payout_type":   SKIPPED,
        "title_field":   SKIPPED,
        "zoom_link":     SKIPPED,
        "tags":          SKIPPED,
        "mandatory":     SKIPPED,
        "show_feedback": SKIPPED,
        "test_groups":   SKIPPED,
        "save":          SKIPPED,
        "notes":         "",
    }

    page.goto(CREATE_URL)
    page.wait_for_load_state("networkidle")
    _wait_for_form(page)
    page.wait_for_timeout(1_000)

    # 1. Title
    title = str(row.get("title", "")).strip()
    print(f"  1. Title         → '{title}'")
    s["title_field"] = _fill_input_by_label(page, "Title", title)

    # 2. Type
    lec_type = str(row.get("type", "")).strip()
    print(f"  2. Type          → '{lec_type}'")
    s["type"] = _select_dropdown_option(page, "Type", lec_type)

    # 3. Category
    category = str(row.get("category", "")).strip()
    print(f"  3. Category      → '{category}'")
    s["category"] = _select_dropdown_option(page, "Category", category)

    # 4. Module
    module = str(row.get("module", "")).strip()
    print(f"  4. Module        → '{module}'")
    s["module"] = _select_dropdown_option(page, "Module", module)

    # 5. Tags
    tags_raw = str(row.get("tags", "")).strip()
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    print(f"  5. Tags          → {tags}")
    if not tags:
        s["tags"] = SKIPPED
    else:
        any_ok = False
        for tag in tags:
            if _add_multi_tag(page, tag):
                any_ok = True
        s["tags"] = CREATED if any_ok else FAILED

    # 6. Primary Host (lookup name from email)
    host_email = str(row.get("host_email", "")).strip().lower()
    host_name  = hosts_map.get(host_email, "")
    if not host_name:
        print(f"     [WARN] Host email '{host_email}' not found in hosts mapping")
        s["host"]  = FAILED
        s["notes"] = f"Host email not in hosts.csv: {host_email}"
    else:
        print(f"  6. Primary Host  → '{host_name}' (match by email: {host_email})")
        s["host"] = _select_host_by_email(page, host_name, host_email)

    # 6b. Payout type — only required for some hosts (no-op otherwise)
    page.wait_for_timeout(500)  # let the host card finish rendering
    s["payout_type"] = _select_payout_type_if_required(page)

    # 7. Batch
    batch = str(row.get("batch", "")).strip()
    print(f"  7. Batch         → '{batch}'")
    s["batch"] = _select_dropdown_option(page, "Batch", batch)

    # 8. Section (depends on batch — small wait for options to reload)
    section = str(row.get("section", "")).strip()
    if not is_blank(section):
        page.wait_for_timeout(800)
    print(f"  8. Section       → '{section}'")
    s["section"] = _select_dropdown_option(page, "Section", section)

    # 9. Test Groups (Test Group / topic_001 / test_LO_001)
    print(f"  9. Test groups   → setting defaults...")
    s["test_groups"] = _set_schedule_defaults(page)

    # 10. Zoom link
    zoom_link = str(row.get("zoom_link", "")).strip()
    print(f" 10. Zoom Link    → '{zoom_link}'")
    s["zoom_link"] = _fill_input_by_label(page, "Zoom", zoom_link)

    # 11. Mandatory toggle — accept either 'mandatory' (TRUE/FALSE)
    # or 'optional' (yes/no, inverse meaning) from the CSV.
    if not is_blank(row.get("optional", "")):
        mand = not to_bool(row.get("optional"))
        src  = f"optional={row.get('optional')}"
    else:
        mand = to_bool(row.get("mandatory", ""))
        src  = f"mandatory={row.get('mandatory')}"
    print(f" 11. Mandatory    → {'ON' if mand else 'OFF'}  ({src})")
    s["mandatory"] = _set_toggle(page, r"[Mm]andatory|[Oo]ptional", mand, "Mandatory")

    # 12. Show feedback toggle
    fb = to_bool(row.get("show_feedback", ""))
    print(f" 12. ShowFB       → {'ON' if fb else 'OFF'}")
    s["show_feedback"] = _set_toggle(page, r"Show.*Feedback", fb, "ShowFB")

    # 13. Schedule (combined date + time) — set last so other fields can't reset it
    schedule_dt = combine_dt(row.get("schedule_date"), row.get("schedule_time"))
    print(f" 13. Schedule     → '{schedule_dt}'")
    s["schedule"] = _set_datetime(page, "Schedule", schedule_dt)

    # 14. Concludes (combined date + time)
    concludes_dt = combine_dt(row.get("concludes_date"), row.get("concludes_time"))
    print(f" 14. Concludes    → '{concludes_dt}'")
    s["concludes"] = _set_datetime(page, "Conclude", concludes_dt)

    # 14b. Second-pass Payout type check — the "PAYOUT PLAN (EXTERNAL HOSTS)"
    # panel may render only after later fields have caused re-renders.
    # Picking 'live session' here is what enables Create when this panel is
    # required for external hosts.
    page.wait_for_timeout(500)
    late_payout = _select_payout_type_if_required(page)
    if late_payout == CREATED:
        s["payout_type"] = CREATED

    # 15. Final verification — safety net in case anything still reset the dates
    if not is_blank(schedule_dt):
        current = _read_datetime(page, "Schedule")
        if current != schedule_dt:
            print(f"     [VERIFY] Schedule cleared (was '{current}'), re-applying '{schedule_dt}'")
            s["schedule"] = _set_datetime(page, "Schedule", schedule_dt)
    if not is_blank(concludes_dt):
        current = _read_datetime(page, "Conclude")
        if current != concludes_dt:
            print(f"     [VERIFY] Concludes cleared (was '{current}'), re-applying '{concludes_dt}'")
            s["concludes"] = _set_datetime(page, "Conclude", concludes_dt)

    # 16. Save — click "Create" and wait for redirect to lectures list
    print(f"     Submitting...")
    try:
        page.get_by_role("button", name=re.compile(r"^Create$", re.I)).click()
        # Wait for redirect to lectures list (success signal)
        try:
            page.wait_for_url(re.compile(r"/lectures/(\?|$)"), timeout=20_000)
            s["save"] = CREATED
            print(f"     [CREATED]")
        except Exception:
            # No redirect — submission likely failed
            s["save"] = FAILED
            s["notes"] += " | No redirect after Create — submission may have failed"
            print(f"     [SAVE FAILED] No redirect to lectures list")
    except Exception as e:
        s["save"] = FAILED
        s["notes"] += f" | Save error: {e}"
        print(f"     [SAVE FAILED] {e}")

    return s


# ═════════════════════════════════════════════════════════════════════════════
# CSV detection
# ═════════════════════════════════════════════════════════════════════════════

def _classify_csv(path: str) -> str:
    """Return 'data', 'hosts', or 'unknown'."""
    try:
        df = pd.read_csv(path, nrows=1)
        cols = set(df.columns)
        if {"Name", "Email"}.issubset(cols):
            return "hosts"
        if "title" in cols and "batch" in cols:
            return "data"
    except Exception:
        pass
    return "unknown"


# ═════════════════════════════════════════════════════════════════════════════
# Entry point
# ═════════════════════════════════════════════════════════════════════════════

def run():
    csv_files = sorted(glob.glob(os.path.join(INPUT_DIR, "*.csv")))
    if not csv_files:
        print(f"[ERROR] No CSV files found in {INPUT_DIR}/")
        print("Place input CSV (with 'title' column) and hosts CSV (with 'Name', 'Email') in input/")
        return

    # Classify each CSV as data, hosts, or unknown
    data_csvs = []
    hosts_csv = None
    for f in csv_files:
        kind = _classify_csv(f)
        if kind == "data":
            data_csvs.append(f)
        elif kind == "hosts" and hosts_csv is None:
            hosts_csv = f

    if not data_csvs:
        print(f"[ERROR] No data CSV found (needs 'title' and 'batch' columns)")
        return
    if not hosts_csv:
        print(f"[ERROR] No hosts CSV found (needs 'Name' and 'Email' columns)")
        return

    # If multiple data CSVs found, prompt the user to pick (so parallel runs
    # in different terminals can each select a different chunk).
    if len(data_csvs) == 1:
        data_csv = data_csvs[0]
        print(f"Auto-selecting data CSV: {os.path.basename(data_csv)}")
    else:
        print(f"Found {len(data_csvs)} data CSV(s):")
        for i, f in enumerate(data_csvs):
            print(f"  [{i}] {os.path.basename(f)}")
        idx = input("\nEnter file number: ").strip()
        try:
            data_csv = data_csvs[int(idx)]
        except (ValueError, IndexError):
            print("[ERROR] Invalid selection.")
            return

    print(f"Data CSV  → {os.path.basename(data_csv)}")
    print(f"Hosts CSV → {os.path.basename(hosts_csv)}")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base      = os.path.splitext(os.path.basename(data_csv))[0]
    log_stem  = f"run_{base}_{timestamp}"
    _start_log(log_stem)

    df = pd.read_csv(data_csv)
    required = {"title", "batch", "section", "category", "tags", "type",
                "schedule_date", "schedule_time", "concludes_date", "concludes_time",
                "host_email", "zoom_link", "module", "show_feedback"}
    missing = required - set(df.columns)
    # Need either 'mandatory' (true/false) or 'optional' (yes/no — inverse)
    if "mandatory" not in df.columns and "optional" not in df.columns:
        missing.add("mandatory or optional")
    if missing:
        print(f"[ERROR] Data CSV missing columns: {missing}")
        _stop_log()
        return

    hosts_df = pd.read_csv(hosts_csv)
    hosts_map = dict(zip(
        hosts_df["Email"].astype(str).str.strip().str.lower(),
        hosts_df["Name"].astype(str).str.strip(),
    ))
    print(f"Loaded {len(hosts_map)} host(s) from hosts CSV")
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
            print("Logged in. Starting lecture creation...\n")
        except Exception as login_err:
            print(f"[WARN] Auto-login failed: {login_err}")
            input("Please log in manually, then press ENTER... ")
            page.wait_for_load_state("networkidle", timeout=20_000)
            print("Resuming...\n")

        for i, row in df.iterrows():
            print(f"{'─'*60}")
            print(f"[{i+1}/{len(df)}] {row.get('title', '')[:60]}")
            try:
                result = create_lecture(page, row, hosts_map)
            except Exception as e:
                print(f"  [ERROR] {e}")
                result = {k: ERROR for k in
                          ["title_field","batch","section","category","module","type",
                           "schedule","concludes","host","payout_type","zoom_link","tags",
                           "mandatory","show_feedback","test_groups","save"]}
                result["title"] = row.get("title", "")
                result["notes"] = str(e)
            all_results.append(result)
            print()

        browser.close()

    csv_path = os.path.join(LOGS_DIR, f"{log_stem}.csv")
    pd.DataFrame(all_results).to_csv(csv_path, index=False)
    print(f"\nCSV report  → {csv_path}")

    dest = os.path.join(ARCHIVE_DIR, f"{base}_{timestamp}.csv")
    shutil.copy2(data_csv, dest)
    print(f"Input archived → {dest}")

    df_log = pd.DataFrame(all_results)
    print("\n══ Summary ══════════════════════════════════════════════")
    field_cols = ["title_field","batch","section","category","module","type",
                  "schedule","concludes","host","payout_type","zoom_link","tags",
                  "mandatory","show_feedback","test_groups","save"]
    for col in field_cols:
        if col in df_log.columns:
            print(f"  {col:20s}: {df_log[col].value_counts().to_dict()}")

    skip_keys = {"notes", "title"}
    failed = [s for s in all_results
              if any(v in (FAILED, ERROR) for k, v in s.items() if k not in skip_keys)]
    not_created_idx = [i for i, s in enumerate(all_results) if s.get("save") != CREATED]
    saved_with_warnings = len(failed) - len(not_created_idx)

    print(f"\n  Total: {len(all_results)}  "
          f"|  Created: {len(all_results) - len(not_created_idx)}  "
          f"|  Not created: {len(not_created_idx)}  "
          f"|  Created with field warnings: {max(saved_with_warnings, 0)}")

    if failed:
        print("\n  ── Failed / Error lectures ──────────────────────────")
        for s in failed:
            t    = str(s.get("title", ""))[:50]
            bad  = {k: v for k, v in s.items() if k not in skip_keys and v in (FAILED, ERROR)}
            note = s.get("notes", "")
            line = f"    [{t}]  {bad}"
            if note:
                line += f"  — {note}"
            print(line)
        print("  ─────────────────────────────────────────────────────")

    # Auto-generate a retry CSV for any lectures that didn't get created.
    # Dropped into input/ so a follow-up run can pick it up directly.
    if not_created_idx:
        retry_df   = df.iloc[not_created_idx].reset_index(drop=True)
        retry_path = os.path.join(INPUT_DIR, f"retry_{base}_{timestamp}.csv")
        retry_df.to_csv(retry_path, index=False)
        print(f"\n  Retry CSV ({len(retry_df)} not-created lectures) → {retry_path}")
        print(f"  To re-run just these, move the original CSV out of input/ "
              f"and run the script again.")

    print("═════════════════════════════════════════════════════════")
    print("Done.")
    _stop_log()


if __name__ == "__main__":
    run()
