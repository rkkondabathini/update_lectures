"""
send_messages.py — Bulk LMS message sender
Site: https://experience-admin.masaischool.com/messages/

Uses a DIFFERENT account than the rest of the tools (Student Experience).

REQUIRED CSV columns:
  title              — message title
  category           — react-select category (e.g. 'General')
  type               — react-select type (e.g. 'Information')
  schedule           — datetime-local format, e.g. '2026-05-31T13:30'
  concludes          — datetime-local format, e.g. '2026-06-01T13:30'
  student_codes      — whitespace-separated list of student codes
  message_body       — full message body text (supports multi-line via \\n)

OPTIONAL CSV columns:
  show_as_popup      — TRUE / FALSE (default FALSE)
  cta_name           — call-to-action button label
  cta_link           — call-to-action URL

Place CSV in ./input/ and run:
  python send_messages.py
"""

import os
import re
import sys
import glob
import shutil
import pandas as pd
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ── Directories ───────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
INPUT_DIR   = os.path.join(BASE_DIR, "input")
LOGS_DIR    = os.path.join(BASE_DIR, "logs")
ARCHIVE_DIR = os.path.join(LOGS_DIR, "archive")

for d in (INPUT_DIR, LOGS_DIR, ARCHIVE_DIR):
    os.makedirs(d, exist_ok=True)

# ── Credentials / URLs ────────────────────────────────────────────────────────
# Student Experience account — different from the other scripts in this repo.
LOGIN_URL    = "https://experience-admin.masaischool.com/"
MESSAGES_URL = "https://experience-admin.masaischool.com/messages/"
EMAIL        = "studentexperience@masaischool.com"
PASSWORD     = "MaS@!4321"

# ── Status constants ──────────────────────────────────────────────────────────
SKIPPED = "SKIPPED"
SENT    = "SENT"
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


def _truthy(val) -> bool:
    if is_blank(val):
        return False
    return str(val).strip().lower() in {"true", "1", "yes", "y", "t"}


def _to_datetime_local(val: str, field_name: str) -> str:
    """Convert a date string to the 'YYYY-MM-DDTHH:MM' format that the
    datetime-local <input> requires. Accepts DD/MM/YYYY, YYYY-MM-DD, etc."""
    s = str(val).strip()
    if not s:
        return s
    # Already in datetime-local format
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", s):
        return s
    try:
        dt = pd.to_datetime(s, dayfirst=True, errors="raise")
        return dt.strftime("%Y-%m-%dT%H:%M")
    except Exception as e:
        raise ValueError(f"{field_name}: cannot parse '{s}' as a date/time ({e})")


def _login(page) -> None:
    print(f"Attempting auto-login as {EMAIL}...")
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


def _pick_react_select(page, combo_name_pattern: str, value: str, field_label: str) -> None:
    """Type into a react-select and click an exact-text option."""
    combo = page.get_by_role("combobox", name=re.compile(combo_name_pattern, re.I)).first
    # Open the dropdown via its input container; fall back to clicking the combo directly.
    try:
        page.locator(".react-select__input-container").first.click()
        page.wait_for_timeout(200)
    except Exception:
        pass
    combo.fill(value)
    page.wait_for_timeout(600)
    option = page.locator(".react-select__option").filter(
        has_text=re.compile(rf"^{re.escape(value)}$", re.I)
    )
    if option.count() == 0:
        # Try the plain text fallback
        page.get_by_text(value, exact=True).first.click()
    else:
        option.first.click()
    page.wait_for_timeout(300)
    print(f"     {field_label} = '{value}'")


# ── Per-row processor ─────────────────────────────────────────────────────────
def process_row(page, row) -> dict:
    title     = str(row.get("title", "")).strip()
    category  = str(row.get("category", "")).strip()
    msg_type  = str(row.get("type", "")).strip()
    schedule  = str(row.get("schedule", "")).strip()
    concludes = str(row.get("concludes", "")).strip()

    # Convert dates to datetime-local format ('YYYY-MM-DDTHH:MM')
    try:
        if schedule:
            schedule = _to_datetime_local(schedule, "schedule")
        if concludes:
            concludes = _to_datetime_local(concludes, "concludes")
    except ValueError as ve:
        result = {"title": title, "status": FAILED, "notes": str(ve)}
        print(f"     [FAIL] {ve}")
        return result
    codes     = str(row.get("student_codes", "")).strip()
    body      = str(row.get("message_body", ""))
    popup     = _truthy(row.get("show_as_popup", ""))
    cta_name  = str(row.get("cta_name", "")).strip()
    cta_link  = str(row.get("cta_link", "")).strip()

    result = {
        "title":    title,
        "status":   SKIPPED,
        "notes":    "",
    }

    missing = [k for k, v in {
        "title": title, "category": category, "type": msg_type,
        "schedule": schedule, "concludes": concludes,
        "student_codes": codes, "message_body": body.strip(),
    }.items() if not str(v).strip()]
    if missing:
        result["status"] = FAILED
        result["notes"] = f"missing required: {missing}"
        print(f"     [SKIP] missing required: {missing}")
        return result

    try:
        page.goto(MESSAGES_URL)
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(800)

        page.get_by_role("button", name="Send a new message").click()
        page.wait_for_timeout(800)

        # Title
        page.get_by_role("textbox", name="Enter title").click()
        page.get_by_role("textbox", name="Enter title").fill(title)
        print(f"     title = '{title[:60]}{'…' if len(title) > 60 else ''}'")

        # Category (react-select)
        _pick_react_select(page, r"Category", category, "category")

        # Type (react-select)
        _pick_react_select(page, r"Type", msg_type, "type")

        # Schedule (datetime-local)
        page.get_by_role("textbox", name=re.compile(r"^Schedule", re.I)).first.fill(schedule)
        page.keyboard.press("Tab")
        page.wait_for_timeout(200)
        print(f"     schedule = '{schedule}'")

        # Concludes (datetime-local)
        page.get_by_role("textbox", name=re.compile(r"^Concludes", re.I)).first.fill(concludes)
        page.keyboard.press("Tab")
        page.wait_for_timeout(200)
        print(f"     concludes = '{concludes}'")

        # Recipients (student codes) — placeholder shows sample IDs
        recipients = page.get_by_role("textbox",
                                      name=re.compile(r"fw\d+_", re.I)).first
        recipients.click()
        recipients.fill(codes)
        n_codes = len([c for c in re.split(r"\s+", codes) if c])
        print(f"     student_codes count = {n_codes}")

        # Message body — falls back to the 5th textbox (matches codegen recording)
        body_box = page.locator("textarea").last
        if body_box.count() == 0:
            body_box = page.get_by_role("textbox").nth(4)
        body_box.click()
        body_box.fill(body)
        print(f"     message_body length = {len(body)} chars")

        # Optional: show as popup
        if popup:
            page.get_by_text("Show as popup").click()
            print(f"     show_as_popup = TRUE")

        # Optional: CTA
        if cta_name:
            page.get_by_role("textbox", name="CTA Name").click()
            page.get_by_role("textbox", name="CTA Name").fill(cta_name)
            print(f"     cta_name = '{cta_name}'")
        if cta_link:
            page.get_by_role("textbox", name="CTA Link").click()
            page.get_by_role("textbox", name="CTA Link").fill(cta_link)
            print(f"     cta_link = '{cta_link}'")

        # Submit
        page.get_by_role("button", name="Submit").click()

        # Wait for success toast/text
        try:
            page.get_by_text(re.compile(r"Message has been sent", re.I)).wait_for(
                state="visible", timeout=30_000
            )
            print(f"     [SENT] success confirmation received")
            result["status"] = SENT
        except PlaywrightTimeoutError:
            result["status"] = FAILED
            result["notes"] = "no 'Message has been sent' confirmation within 30s"
            print(f"     [FAIL] no success confirmation within 30s")
            return result

        # Settle before next row
        page.wait_for_timeout(1_500)
        return result

    except PlaywrightTimeoutError as e:
        print(f"     [ERROR] timeout: {e}")
        result["status"] = ERROR
        result["notes"] = f"timeout: {e}"
        return result
    except Exception as e:
        print(f"     [ERROR] {e}")
        result["status"] = ERROR
        result["notes"] = str(e)
        return result


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
    df.columns = [c.strip() for c in df.columns]

    print(f"Rows to process: {len(df)}\n")
    all_results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=200)
        context = browser.new_context()
        page    = context.new_page()

        _login(page)

        for i, row in df.iterrows():
            print(f"{'─'*60}")
            print(f"[{i+1}/{len(df)}] '{str(row.get('title','')).strip()[:80]}'")
            try:
                result = process_row(page, row)
            except Exception as e:
                print(f"  [ERROR] {e}")
                result = {
                    "title":  str(row.get("title", "")).strip(),
                    "status": ERROR,
                    "notes":  str(e),
                }
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
    vc = df_log["status"].value_counts().to_dict()
    print(f"  status: {vc}")

    failed = [r for r in all_results if r["status"] in (FAILED, ERROR)]
    print(f"\n  Rows with failures/errors: {len(failed)}/{len(all_results)}")
    if failed:
        print("\n  ── Failed / Error rows ───────────────────────────────")
        for r in failed:
            line = f"    [{r['title'][:60]}]  {r['status']}"
            if r.get("notes"):
                line += f"  — {r['notes']}"
            print(line)
        print("  ─────────────────────────────────────────────────────")

    print("═════════════════════════════════════════════════════════")
    print("Done.")
    _stop_log()


if __name__ == "__main__":
    run()
