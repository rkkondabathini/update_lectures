"""
delete_lecture.py — Bulk lecture deleter

⚠️  DESTRUCTIVE. Cannot be undone. Requires explicit confirmation before running.

Required CSV column:
  lecture_id — numeric lecture ID (URL is built automatically)

Place CSV in ./input/ and run:
  python delete_lecture.py
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

# ── Credentials ───────────────────────────────────────────────────────────────
LOGIN_URL    = "https://experience-admin.masaischool.com/"
LECTURE_TMPL = "https://experience-admin.masaischool.com/lectures/detail/?id={lecture_id}"
EMAIL        = "ravi.kiran@masaischool.com"
PASSWORD     = "mAs@!4321"

# ── Status ────────────────────────────────────────────────────────────────────
SKIPPED = "SKIPPED"
DELETED = "DELETED"
FAILED  = "FAILED"
ERROR   = "ERROR"

CONFIRM_TEXT = "Are you sure you want to delete this Lecture?"


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


# ═════════════════════════════════════════════════════════════════════════════
# Per-lecture deleter
# ═════════════════════════════════════════════════════════════════════════════

def delete_lecture(page, lecture_id: str) -> dict:
    url = LECTURE_TMPL.format(lecture_id=lecture_id)
    s = {"lecture_id": lecture_id, "delete": SKIPPED, "notes": ""}

    page.goto(url, timeout=60_000)
    page.wait_for_load_state("networkidle", timeout=30_000)
    page.wait_for_timeout(1_500)

    # Step 1 — click the trash icon (the only red-bordered button in the action row).
    # The button has no visible text, so name="Delete" wouldn't match unless aria-label
    # is set. Try several selectors in priority order; first match wins.
    trash_selectors = [
        "button[class*='border-red']",   # Tailwind red-bordered button (most likely)
        "button[class*='text-red']",     # red-tinted icon button
        "button:has(svg.lucide-trash-2)",
        "button:has(svg.lucide-trash)",
        "button[aria-label='Delete']",
        "button[title='Delete']",
    ]
    trash_btn = None
    for sel in trash_selectors:
        cand = page.locator(sel)
        try:
            if cand.count() > 0 and cand.first.is_visible(timeout=500):
                trash_btn = cand.first
                break
        except Exception:
            continue

    if trash_btn is None:
        s["delete"] = FAILED
        s["notes"]  = "Trash icon button not found (none of the selectors matched)"
        print(f"  [FAILED] Trash icon not found")
        return s

    try:
        trash_btn.click()
    except Exception as e:
        s["delete"] = FAILED
        s["notes"]  = f"Trash icon click failed: {e}"
        print(f"  [FAILED] Could not click trash icon: {e}")
        return s

    # Step 2 — wait for the confirmation modal to appear
    try:
        page.get_by_text(CONFIRM_TEXT).wait_for(state="visible", timeout=8_000)
    except Exception as e:
        s["delete"] = FAILED
        s["notes"]  = f"Confirmation modal didn't appear: {e}"
        print(f"  [FAILED] Confirmation modal not visible: {e}")
        return s

    # Step 3 — click the red Delete button inside the modal.
    # The trash icon has no visible text "Delete" (it's an SVG-only button),
    # so a button with accessible name "Delete" should ONLY be the modal one.
    # Try several selectors; first that finds a visible match wins.
    modal_delete = None
    for sel_fn in [
        lambda: page.get_by_role("button", name="Delete", exact=True),
        lambda: page.locator("button:has-text('Delete')"),
        lambda: page.locator("button[class*='bg-red']"),
    ]:
        try:
            cand = sel_fn()
            count = cand.count()
            for i in range(count):
                btn = cand.nth(i)
                if btn.is_visible(timeout=300):
                    modal_delete = btn
            if modal_delete:
                break
        except Exception:
            continue

    if modal_delete is None:
        s["delete"] = FAILED
        s["notes"]  = "Modal Delete button not found"
        print(f"  [FAILED] Modal Delete button not found")
        return s

    try:
        modal_delete.click()
    except Exception as e:
        s["delete"] = FAILED
        s["notes"]  = f"Modal Delete click failed: {e}"
        print(f"  [FAILED] Could not click modal Delete: {e}")
        return s

    # Step 4 — verify the delete actually happened: either the modal closes,
    # OR the page redirects away from the lecture detail URL.
    try:
        page.wait_for_function(
            f"""() => {{
                const txt = document.body.innerText || '';
                const stillOnDetail = window.location.pathname.includes('/lectures/detail');
                const modalGone = !txt.includes({repr(CONFIRM_TEXT)});
                return modalGone || !stillOnDetail;
            }}""",
            timeout=10_000,
        )
        page.wait_for_timeout(600)
        s["delete"] = DELETED
        print(f"  [DELETED]")
    except Exception as e:
        s["delete"] = FAILED
        s["notes"]  = f"No success signal after Delete click: {e}"
        print(f"  [FAILED] No success signal after Delete: {e}")

    return s


# ═════════════════════════════════════════════════════════════════════════════
# Entry point
# ═════════════════════════════════════════════════════════════════════════════

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

    df = pd.read_csv(chosen, dtype=str)
    if "lecture_id" not in df.columns:
        print("[ERROR] CSV must have a 'lecture_id' column")
        return

    n = len(df)
    print(f"\n{'═'*60}")
    print(f"⚠️   ABOUT TO DELETE {n} LECTURE(S). THIS CANNOT BE UNDONE.")
    print(f"{'═'*60}")
    print("First few IDs:")
    for lid in df["lecture_id"].head(5):
        print(f"  {lid}")
    if n > 5:
        print(f"  ... and {n - 5} more")

    expected = f"DELETE {n}"
    confirm = input(f"\nType '{expected}' to proceed (anything else cancels): ").strip()
    if confirm != expected:
        print("Cancelled. No deletions performed.")
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base      = os.path.splitext(os.path.basename(chosen))[0]
    log_stem  = f"run_{base}_{timestamp}"
    _start_log(log_stem)

    print(f"\nProceeding with deletion of {n} lecture(s)...\n")

    all_results = []

    def _login(pg):
        print("Attempting auto-login...")
        try:
            pg.goto(LOGIN_URL)
            pg.wait_for_load_state("networkidle")
            pg.get_by_role("textbox", name="Your email").fill(EMAIL)
            pg.get_by_role("textbox", name="Your email").press("Tab")
            pg.get_by_role("textbox", name="Your password").fill(PASSWORD)
            pg.locator("svg").click()
            pg.get_by_role("button", name="Sign In").click()
            pg.wait_for_load_state("networkidle", timeout=20_000)
            if "login" in pg.url.lower() or pg.url.rstrip("/") == LOGIN_URL.rstrip("/"):
                raise Exception(f"Still on login page: {pg.url}")
            print("Logged in.\n")
        except Exception as login_err:
            print(f"[WARN] Auto-login failed: {login_err}")
            input("Please log in manually, then press ENTER... ")
            print("Resuming...\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=200)
        context = browser.new_context()
        page    = context.new_page()
        _login(page)

        consecutive_errors = 0
        MAX_CONSECUTIVE    = 5

        for i, row in df.iterrows():
            lecture_id = str(row.get("lecture_id", "")).strip()
            print(f"{'─'*60}")
            print(f"[{i+1}/{n}] id={lecture_id}")
            try:
                result = delete_lecture(page, lecture_id)
                if result["delete"] in (DELETED, SKIPPED):
                    consecutive_errors = 0
                else:
                    consecutive_errors += 1
            except Exception as e:
                err_msg = str(e)
                print(f"  [ERROR] {err_msg}")
                result = {"lecture_id": lecture_id, "delete": ERROR, "notes": err_msg}
                consecutive_errors += 1

                # Browser/page crashed — re-launch
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
                print(f"\n[ABORT] {MAX_CONSECUTIVE} consecutive failures — stopping to avoid wasting time.")
                for remaining_row in df.iloc[i+1:].itertuples():
                    lid = str(getattr(remaining_row, "lecture_id", "")).strip()
                    all_results.append({"lecture_id": lid, "delete": SKIPPED, "notes": "skipped after consecutive failures"})
                break

        try:
            browser.close()
        except Exception:
            pass

    csv_path = os.path.join(LOGS_DIR, f"{log_stem}.csv")
    pd.DataFrame(all_results).to_csv(csv_path, index=False)
    print(f"\nCSV report → {csv_path}")

    dest = os.path.join(ARCHIVE_DIR, f"{base}_{timestamp}.csv")
    shutil.copy2(chosen, dest)
    print(f"Input archived → {dest}")

    df_log = pd.DataFrame(all_results)
    print("\n══ Summary ══════════════════════════════════════════════")
    if "delete" in df_log.columns:
        print(f"  delete: {df_log['delete'].value_counts().to_dict()}")

    failed = [s for s in all_results if s.get("delete") in (FAILED, ERROR)]
    print(f"\n  Lectures with failures/errors: {len(failed)}/{len(all_results)}")

    if failed:
        print("\n  ── Failed / Error lecture IDs ────────────────────────")
        for s in failed:
            lid  = s.get("lecture_id", "")
            note = s.get("notes", "")
            line = f"    [{lid}]  {s.get('delete', '')}"
            if note:
                line += f"  — {note}"
            print(line)
        print("  ─────────────────────────────────────────────────────")

    print("═════════════════════════════════════════════════════════")
    print("Done.")
    _stop_log()


if __name__ == "__main__":
    run()
