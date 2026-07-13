"""
update_student_code.py — Bulk student-code (UserName) updater
Site: https://experience-admin.masaischool.com/Users/

REQUIRED CSV columns (flexible header matching — case/underscores/spaces ignored):
  email or Old Student code   (at least one per row — used to search)
  new student code            (the new UserName to set)

OPTIONAL CSV columns:
  Name                        (kept in report for readability)

Place CSV in ./input/ and run:
  python update_student_code.py
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
LOGIN_URL = "https://experience-admin.masaischool.com/"
USERS_URL = "https://experience-admin.masaischool.com/Users/"
EMAIL     = "ravi.kiran@masaischool.com"
PASSWORD  = "mAs@!4321"

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


# ── CSV column resolution ─────────────────────────────────────────────────────
def _canon(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).strip().lower())


def _pick_column(df: pd.DataFrame, aliases: list[str]) -> str | None:
    wanted = {_canon(a) for a in aliases}
    for col in df.columns:
        if _canon(col) in wanted:
            return col
    return None


def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    col_name  = _pick_column(df, ["Name"])
    col_email = _pick_column(df, ["email", "email id", "mail"])
    col_old   = _pick_column(df, ["Old Student code", "old_student_code", "old code", "username"])
    col_new   = _pick_column(df, ["new student code", "new_student_code", "new code", "updated username"])

    if not col_new:
        raise ValueError("CSV must contain a 'new student code' column.")
    if not col_email and not col_old:
        raise ValueError("CSV must contain either 'email' or 'Old Student code' for searching.")

    out = pd.DataFrame()
    out["name"]             = df[col_name]  if col_name  else ""
    out["email"]            = df[col_email] if col_email else ""
    out["old_student_code"] = df[col_old]   if col_old   else ""
    out["new_student_code"] = df[col_new]
    for c in out.columns:
        out[c] = out[c].fillna("").astype(str).str.strip()
    return out


# ── Login ─────────────────────────────────────────────────────────────────────
def _login(page) -> None:
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


# ── Per-row processor ─────────────────────────────────────────────────────────
def process_row(page, row) -> dict:
    name     = row.get("name", "").strip()
    email    = row.get("email", "").strip()
    old_code = row.get("old_student_code", "").strip()
    new_code = row.get("new_student_code", "").strip()

    result = {
        "name":             name,
        "email":            email,
        "old_student_code": old_code,
        "new_student_code": new_code,
        "username_update":  SKIPPED,
        "notes":            "",
    }

    if not new_code:
        result["username_update"] = SKIPPED
        result["notes"] = "new student code blank"
        return result
    if not email and not old_code:
        result["username_update"] = FAILED
        result["notes"] = "email and old_student_code both blank — nothing to search"
        return result

    query = email if email else old_code
    print(f"  Searching: '{query}'")

    try:
        search = page.get_by_role("textbox", name="Search by code, name, email")
        search.click()
        search.press("ControlOrMeta+a")
        search.fill(query)
        page.wait_for_timeout(5_000)  # let results load — minimum 5s

        # Wait up to 10s more for the Edit button to appear before declaring "not found"
        edit_btn = page.get_by_role("button", name="Edit").first
        try:
            edit_btn.wait_for(state="visible", timeout=10_000)
        except PlaywrightTimeoutError:
            result["username_update"] = FAILED
            result["notes"] = "no Edit button — user not found after 15s wait"
            print(f"     [WARN] user not found (waited 15s)")
            return result
        edit_btn.click()
        page.wait_for_timeout(800)

        username_box = page.get_by_role("textbox", name="UserName")
        username_box.wait_for(state="visible", timeout=8_000)
        current = (username_box.input_value() or "").strip()

        if current == new_code:
            print(f"     [SKIP] already '{new_code}' — closing modal via Update Profile")
            status = SKIPPED
            note   = "already correct"
        else:
            print(f"     UPDATE '{current}' → '{new_code}'")
            username_box.click()
            username_box.press("ControlOrMeta+a")
            username_box.fill(new_code)
            page.wait_for_timeout(300)
            status = CHANGED
            note   = ""

        # Always click Update Profile to close the modal cleanly, then wait for it
        # to actually disappear before returning. This is the fix for the "user
        # not found" cascade — without this, the modal blocks the next search.
        _close_modal_via_update_profile(page, username_box)

        result["username_update"] = status
        result["notes"] = note
        return result

    except PlaywrightTimeoutError as e:
        print(f"     [ERROR] timeout: {e}")
        result["username_update"] = ERROR
        result["notes"] = f"timeout: {e}"
        _force_close_modal(page)
        return result
    except Exception as e:
        print(f"     [ERROR] {e}")
        result["username_update"] = ERROR
        result["notes"] = str(e)
        _force_close_modal(page)
        return result


def _close_modal_via_update_profile(page, username_box):
    """Click 'Update Profile' (fallback to 'Update'), then wait for the Edit
    modal to fully disappear so the next row's search is unblocked."""
    clicked = False
    for btn_name in ("Update Profile", "Update"):
        try:
            btn = page.get_by_role("button", name=re.compile(rf"^{re.escape(btn_name)}$", re.I))
            if btn.count() > 0 and btn.first.is_visible(timeout=1_500):
                btn.first.click()
                print(f"     clicked '{btn_name}'")
                clicked = True
                break
        except Exception:
            continue
    if not clicked:
        print(f"     [WARN] no Update/Update Profile button found — pressing Escape")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass

    # Wait for UserName field to disappear (modal closed)
    try:
        username_box.wait_for(state="hidden", timeout=10_000)
    except Exception:
        print(f"     [WARN] modal still visible after click — force-closing")
        _force_close_modal(page)


def _force_close_modal(page):
    """Best-effort modal dismissal when something went wrong."""
    for _ in range(3):
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        except Exception:
            pass
        try:
            box = page.get_by_role("textbox", name="UserName")
            if box.count() == 0 or not box.first.is_visible(timeout=500):
                return
        except Exception:
            return


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

    try:
        raw = pd.read_csv(chosen, dtype=str)
        df  = _normalize_df(raw)
    except Exception as e:
        print(f"[ERROR] {e}")
        _stop_log()
        return

    print(f"Rows to process: {len(df)}\n")
    all_results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=200)
        context = browser.new_context()
        page    = context.new_page()

        _login(page)

        page.goto(USERS_URL)
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1_000)

        for i, row in df.iterrows():
            print(f"{'─'*60}")
            print(f"[{i+1}/{len(df)}] {row.get('email') or row.get('old_student_code')} → '{row.get('new_student_code')}'")
            try:
                result = process_row(page, row)
            except Exception as e:
                print(f"  [ERROR] {e}")
                result = {
                    "name":             row.get("name", ""),
                    "email":            row.get("email", ""),
                    "old_student_code": row.get("old_student_code", ""),
                    "new_student_code": row.get("new_student_code", ""),
                    "username_update":  ERROR,
                    "notes":            str(e),
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
    vc = df_log["username_update"].value_counts().to_dict()
    print(f"  username_update: {vc}")

    failed = [r for r in all_results if r["username_update"] in (FAILED, ERROR)]
    print(f"\n  Rows with failures/errors: {len(failed)}/{len(all_results)}")
    if failed:
        print("\n  ── Failed / Error rows ───────────────────────────────")
        for r in failed:
            key = r["email"] or r["old_student_code"]
            line = f"    [{key}] → '{r['new_student_code']}'  {r['username_update']}"
            if r.get("notes"):
                line += f"  — {r['notes']}"
            print(line)
        print("  ─────────────────────────────────────────────────────")

    print("═════════════════════════════════════════════════════════")
    print("Done.")
    _stop_log()


if __name__ == "__main__":
    run()
