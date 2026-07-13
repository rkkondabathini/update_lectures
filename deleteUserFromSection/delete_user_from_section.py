"""
delete_user_from_section.py — Bulk user removal from sections

⚠️  DESTRUCTIVE. Cannot be undone. Requires explicit confirmation before running.

Required CSV columns:
  section_id   — numeric section ID
  student_code — student code to remove (one row per student; all codes for the
                 same section are batched into a single Delete Users operation)

Place CSV in ./input/ and run:
  python delete_user_from_section.py
"""

import re
import os
import sys
import glob
import shutil
import argparse
import pandas as pd
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ── Directories ────────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
INPUT_DIR   = os.path.join(BASE_DIR, "input")
LOGS_DIR    = os.path.join(BASE_DIR, "logs")
ARCHIVE_DIR = os.path.join(LOGS_DIR, "archive")

for d in (INPUT_DIR, LOGS_DIR, ARCHIVE_DIR):
    os.makedirs(d, exist_ok=True)

# ── Credentials / URLs ─────────────────────────────────────────────────────────
LOGIN_URL    = "https://experience-admin.masaischool.com/"
SECTION_TMPL = "https://experience-admin.masaischool.com/sections/sectiondetail/?sectionId={section_id}"
EMAIL        = "ravi.kiran@masaischool.com"
PASSWORD     = "mAs@!4321"

# ── Status ─────────────────────────────────────────────────────────────────────
DELETED = "DELETED"
SKIPPED = "SKIPPED"
FAILED  = "FAILED"
ERROR   = "ERROR"


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


# ── Login ──────────────────────────────────────────────────────────────────────
def _login(page):
    print("Logging in...")
    try:
        page.goto(LOGIN_URL)
        page.wait_for_load_state("networkidle")
        page.get_by_role("textbox", name="Your email").fill(EMAIL)
        page.get_by_role("textbox", name="Your email").press("Tab")
        page.get_by_role("textbox", name="Your password").fill(PASSWORD)
        page.locator("svg").click()   # toggle show-password eye icon
        page.get_by_role("button", name="Sign In").click()
        page.wait_for_load_state("networkidle", timeout=20_000)
        if "login" in page.url.lower() or page.url.rstrip("/") == LOGIN_URL.rstrip("/"):
            raise Exception(f"Still on login page: {page.url}")
        print("Logged in.\n")
    except Exception as e:
        print(f"[WARN] Auto-login failed: {e}")
        input("Log in manually, then press ENTER… ")
        print("Resuming.\n")


# ── Per-section deletion ────────────────────────────────────────────────────────
def delete_users_from_section(page, section_id: str, codes: list) -> dict:
    url = SECTION_TMPL.format(section_id=section_id)
    s   = {
        "section_id":     section_id,
        "users_targeted": len(codes),
        "status":         FAILED,
        "notes":          "",
    }

    page.goto(url, timeout=60_000)
    page.wait_for_load_state("networkidle", timeout=30_000)
    page.wait_for_timeout(1_500)

    # Step 1 — click "Delete users" button to open the panel
    try:
        btn = page.get_by_role("button", name="Delete users")
        btn.wait_for(state="visible", timeout=10_000)
        btn.click()
    except Exception as e:
        s["notes"] = f"'Delete users' button not found: {e}"
        print(f"  [FAILED] {s['notes']}")
        return s

    # Step 2 — fill the student codes textbox (comma-separated)
    try:
        textbox = page.get_by_role("textbox", name="student code")
        textbox.wait_for(state="visible", timeout=8_000)
        textbox.fill(",".join(codes))
        page.wait_for_timeout(500)
    except Exception as e:
        s["notes"] = f"Student code textbox not found: {e}"
        print(f"  [FAILED] {s['notes']}")
        return s

    # Step 3 — click Delete in the "delete User" modal to trigger the confirmation dialog.
    try:
        delete_btn = page.get_by_role("button", name="Delete", exact=True).first
        delete_btn.wait_for(state="visible", timeout=8_000)
        delete_btn.click()
        page.wait_for_timeout(500)
    except Exception as e:
        s["notes"] = f"Modal Delete button not clickable: {e}"
        print(f"  [FAILED] {s['notes']}")
        return s

    # Step 4 — "Are you sure?" dialog appeared; click Delete to confirm.
    # Don't wait on heading text (can vary); just wait for the button to be clickable.
    try:
        page.wait_for_timeout(1_000)
        confirm_btn = page.get_by_role("button", name="Delete").last
        confirm_btn.wait_for(state="visible", timeout=8_000)
        confirm_btn.click()
        page.wait_for_timeout(500)
    except Exception as e:
        s["notes"] = f"Confirmation Delete button not clickable: {e}"
        print(f"  [FAILED] {s['notes']}")
        return s

    # Step 5 — wait for success toast OR "do not exist" error (both are toasts).
    try:
        page.wait_for_function(
            """() => {
                const t = document.body.innerText || '';
                return t.includes('Users has been removed') ||
                       t.includes('do not exist');
            }""",
            timeout=15_000,
        )
        body = page.locator("body").inner_text()
        if "Users has been removed" in body:
            s["status"] = DELETED
            print(f"  [DELETED] {len(codes)} user(s) removed")
        else:
            s["status"] = SKIPPED
            s["notes"]  = "Users do not exist in section (already removed or never enrolled)"
            print(f"  [SKIPPED] {s['notes']}")
    except Exception as e:
        s["notes"] = f"No success/error signal after deletion: {e}"
        print(f"  [FAILED] {s['notes']}")

    return s


# ── Entry point ────────────────────────────────────────────────────────────────
def run():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--start-section", metavar="SECTION_ID",
        help="Skip all sections before this ID (use to resume after a partial run)",
    )
    args = parser.parse_args()
    start_section = args.start_section.strip() if args.start_section else None

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

    df = pd.read_csv(chosen, dtype=str).fillna("")
    missing = {"section_id", "student_code"} - set(df.columns)
    if missing:
        print(f"[ERROR] CSV missing required column(s): {', '.join(sorted(missing))}")
        return

    df["section_id"]   = df["section_id"].str.strip()
    df["student_code"] = df["student_code"].str.strip()
    df = df[(df["section_id"] != "") & (df["student_code"] != "")]

    # Each row's student_code may be a comma-joined list already (one row per section)
    # or individual codes (one row per student). Handle both by splitting then grouping.
    grouped: dict = {}
    for _, row in df.iterrows():
        sid   = row["section_id"]
        codes = [c.strip() for c in row["student_code"].split(",") if c.strip()]
        grouped.setdefault(sid, []).extend(codes)

    sections_list = list(grouped.items())  # preserve CSV order

    # --start-section: skip already-processed sections
    if start_section:
        ids = [sid for sid, _ in sections_list]
        if start_section not in ids:
            print(f"[ERROR] --start-section {start_section!r} not found in CSV. Available: {ids}")
            return
        skip_n       = ids.index(start_section)
        sections_list = sections_list[skip_n:]
        print(f"Skipping {skip_n} already-done section(s) — resuming from {start_section}.\n")

    n_sections = len(sections_list)
    n_users    = sum(len(codes) for _, codes in sections_list)

    print(f"\n{'═'*60}")
    print(f"⚠️   ABOUT TO REMOVE {n_users} USER(S) FROM {n_sections} SECTION(S). THIS CANNOT BE UNDONE.")
    print(f"{'═'*60}")
    print("Sections to process:")
    for sid, codes in sections_list[:5]:
        print(f"  section {sid}: {len(codes)} user(s)")
    if n_sections > 5:
        print(f"  ... and {n_sections - 5} more")

    expected = f"DELETE {n_sections}"
    confirm  = input(f"\nType '{expected}' to proceed (anything else cancels): ").strip()
    if confirm != expected:
        print("Cancelled. No deletions performed.")
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base      = os.path.splitext(os.path.basename(chosen))[0]
    log_stem  = f"run_{base}_{timestamp}"
    _start_log(log_stem)

    print(f"\nProceeding — {n_sections} section(s), {n_users} user(s) total...\n")

    all_results        = []
    consecutive_errors = 0
    MAX_CONSECUTIVE    = 5

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=200)
        context = browser.new_context()
        page    = context.new_page()
        _login(page)

        for idx, (section_id, codes) in enumerate(sections_list, start=1):
            print(f"{'─'*60}")
            print(f"[{idx}/{n_sections}] section={section_id}  users={len(codes)}")
            try:
                result = delete_users_from_section(page, section_id, codes)
                consecutive_errors = 0 if result["status"] in (DELETED, SKIPPED) else consecutive_errors + 1
            except Exception as e:
                err_msg = str(e)
                print(f"  [ERROR] {err_msg}")
                result = {
                    "section_id":     section_id,
                    "users_targeted": len(codes),
                    "status":         ERROR,
                    "notes":          err_msg,
                }
                consecutive_errors += 1

                if "has been closed" in err_msg or "Target closed" in err_msg:
                    print("  [RECOVERY] Browser crashed — re-launching...")
                    try:
                        browser.close()
                    except Exception:
                        pass
                    browser = p.chromium.launch(headless=False, slow_mo=200)
                    context = browser.new_context()
                    page    = context.new_page()
                    _login(page)
                    consecutive_errors = 0

            all_results.append(result)
            print()

            if consecutive_errors >= MAX_CONSECUTIVE:
                print(f"\n[ABORT] {MAX_CONSECUTIVE} consecutive failures — stopping.")
                for rem_sid, rem_codes in sections_list[idx:]:
                    all_results.append({
                        "section_id":     rem_sid,
                        "users_targeted": len(rem_codes),
                        "status":         "SKIPPED",
                        "notes":          "skipped after consecutive failures",
                    })
                break

        try:
            browser.close()
        except Exception:
            pass

    # ── Write results ──────────────────────────────────────────────────────────
    csv_path = os.path.join(LOGS_DIR, f"{log_stem}.csv")
    pd.DataFrame(all_results).to_csv(csv_path, index=False)
    print(f"\nCSV report → {csv_path}")

    dest = os.path.join(ARCHIVE_DIR, f"{base}_{timestamp}.csv")
    shutil.copy2(chosen, dest)
    print(f"Input archived → {dest}")

    df_log = pd.DataFrame(all_results)
    print("\n══ Summary ══════════════════════════════════════════════")
    print(f"  {df_log['status'].value_counts().to_dict()}")

    failed = [r for r in all_results if r.get("status") in (FAILED, ERROR)]
    print(f"\n  Sections with failures/errors: {len(failed)}/{len(all_results)}")
    if failed:
        print("\n  ── Failed / Error section IDs ────────────────────────")
        for r in failed:
            line = f"    [{r.get('section_id', '')}]  {r.get('status', '')}"
            if r.get("notes"):
                line += f"  — {r['notes']}"
            print(line)
        print("  ─────────────────────────────────────────────────────")

    print("═════════════════════════════════════════════════════════")
    print("Done.")
    _stop_log()


if __name__ == "__main__":
    run()
