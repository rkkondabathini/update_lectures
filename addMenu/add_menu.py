"""
add_menu.py — Bulk menu-item creator
Site: https://experience-admin.masaischool.com/menu/

REQUIRED CSV columns:
  outer_category   — parent category of inner_category (e.g. 'tickets-category')
  inner_category   — the category that holds the value (e.g. 'Lecture & Attendance query')
  value            — the menu-item text to create inside inner_category

Two-phase workflow:
  Phase 1 — For each unique (outer, inner) pair: check via MENU_URL search whether
            `inner` already exists. If not, open `outer` and create `inner` inside it.
  Phase 2 — For each row: open `inner` directly via MENU_URL search and create
            `value` inside it (consecutive same-inner rows skip re-navigation).

Place CSV in ./input/ and run:
  python add_menu.py
"""

import os
import re
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
LOGIN_URL = "https://experience-admin.masaischool.com/"
MENU_URL  = "https://experience-admin.masaischool.com/menu/"
EMAIL     = "ravi.kiran@masaischool.com"
PASSWORD  = "mAs@!4321"

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


def _search_at_menu_url(page, category: str):
    """Go to MENU_URL, type `category` in the search box, wait for results.
    Returns the search Locator for the matching link (may be 0 count)."""
    page.goto(MENU_URL)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(800)

    search = page.get_by_role("textbox", name="Search by category name")
    search.click()
    search.fill(category)
    page.wait_for_timeout(900)

    link = page.get_by_role("link", name=re.compile(rf"^{re.escape(category)}$"))
    return link


def _open_category(page, category: str) -> bool:
    """Search for `category` at MENU_URL and click into it."""
    link = _search_at_menu_url(page, category)
    if link.count() == 0:
        print(f"     [WARN] Category not found in search: '{category}'")
        return False
    link.first.click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(800)
    return True


def _fill_modal_category(page, parent_category: str) -> None:
    """Inside the CREATE MENU ITEM modal, set the Category dropdown to
    `parent_category` (selecting the existing option). If the field already
    holds the right value, leave it alone."""
    combo = page.get_by_role("combobox", name="Category (select existing or")

    # Check existing value via the react-select singleValue label
    try:
        single = page.locator(".react-select__single-value").first
        if single.count() > 0 and single.inner_text(timeout=1_000).strip() == parent_category:
            return  # already set correctly
    except Exception:
        pass

    page.locator(".react-select__input-container").first.click()
    page.wait_for_timeout(200)
    combo.fill(parent_category)
    page.wait_for_timeout(700)

    option = page.locator(".react-select__option").filter(
        has_text=re.compile(rf"^{re.escape(parent_category)}$")
    )
    if option.count() == 0:
        raise Exception(f"existing Category option '{parent_category}' not found in dropdown")
    option.first.click()
    page.wait_for_timeout(300)


def _create_menu_item(page, parent_category: str, value: str) -> str:
    """On the current category page, click CREATE MENU ITEM and submit
    {Category=parent_category, Value=value}."""
    try:
        page.get_by_role("button", name="CREATE MENU ITEM").click()
        page.wait_for_timeout(600)

        _fill_modal_category(page, parent_category)

        val_box = page.get_by_role("textbox", name="Value")
        val_box.click()
        val_box.fill(value)
        page.wait_for_timeout(300)

        page.get_by_role("button", name="Create").click()
        page.wait_for_load_state("networkidle", timeout=15_000)
        page.wait_for_timeout(800)
        return CREATED
    except Exception as e:
        print(f"     [WARN] Create failed: {e}")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return FAILED


# ── Phase 1: ensure inner categories exist ───────────────────────────────────
def phase1_ensure_inner(page, outer: str, inner: str) -> str:
    """Check (via MENU_URL search) whether `inner` exists. If not, open
    `outer` and create `inner` inside it."""
    link = _search_at_menu_url(page, inner)
    if link.count() > 0:
        print(f"  [SKIP] inner_category already exists: '{inner}'")
        return SKIPPED

    print(f"  Creating inner_category '{inner}' under '{outer}'")
    if not _open_category(page, outer):
        return FAILED
    return _create_menu_item(page, outer, inner)


# ── Phase 2: create one value under its inner_category ───────────────────────
def phase2_create_value(page, inner: str, value: str, current_inner: str) -> tuple[str, str]:
    """Create `value` inside `inner`. Returns (status, new_current_inner)."""
    if current_inner != inner:
        if not _open_category(page, inner):
            return FAILED, ""
        current_inner = inner
    status = _create_menu_item(page, inner, value)
    if status != CREATED:
        # Modal may have left the page in an odd state — force re-open next time.
        current_inner = ""
    return status, current_inner


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
    df.columns = [c.strip() for c in df.columns]  # tolerate "outer_category, inner_category, value"
    required = {"outer_category", "inner_category", "value"}
    missing  = required - set(df.columns)
    if missing:
        print(f"[ERROR] CSV missing required columns: {sorted(missing)}")
        _stop_log()
        return

    # Drop rows with any blank required field
    df = df.copy()
    for col in ("outer_category", "inner_category", "value"):
        df[col] = df[col].fillna("").astype(str).str.strip()
    blanks = df[(df["outer_category"] == "") | (df["inner_category"] == "") | (df["value"] == "")]
    if len(blanks) > 0:
        print(f"[WARN] Skipping {len(blanks)} row(s) with blank required fields")
    df = df[(df["outer_category"] != "") & (df["inner_category"] != "") & (df["value"] != "")]

    unique_pairs = df[["outer_category", "inner_category"]].drop_duplicates().reset_index(drop=True)
    print(f"Rows to process: {len(df)}")
    print(f"Unique (outer, inner) pairs: {len(unique_pairs)}\n")

    phase1_status: dict[tuple[str, str], str] = {}
    all_results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=200)
        context = browser.new_context()
        page    = context.new_page()

        _login(page)

        # ── Phase 1 ───────────────────────────────────────────────────────────
        print("══ Phase 1: ensuring inner categories ══════════════════")
        for i, pair in unique_pairs.iterrows():
            outer = pair["outer_category"]
            inner = pair["inner_category"]
            print(f"{'─'*60}")
            print(f"[{i+1}/{len(unique_pairs)}] {outer} / {inner}")
            try:
                status = phase1_ensure_inner(page, outer, inner)
            except Exception as e:
                print(f"  [ERROR] {e}")
                status = ERROR
            phase1_status[(outer, inner)] = status

        # ── Phase 2 ───────────────────────────────────────────────────────────
        print(f"\n══ Phase 2: creating values ════════════════════════════")
        current_inner = ""
        for i, row in df.reset_index(drop=True).iterrows():
            outer = row["outer_category"]
            inner = row["inner_category"]
            value = row["value"]
            print(f"{'─'*60}")
            print(f"[{i+1}/{len(df)}] {inner} ← '{value}'")

            result = {
                "outer_category": outer,
                "inner_category": inner,
                "value":          value,
                "status":         SKIPPED,
                "notes":          "",
            }

            # Skip if Phase 1 failed for this inner_category
            p1 = phase1_status.get((outer, inner), SKIPPED)
            if p1 in (FAILED, ERROR):
                result["status"] = SKIPPED
                result["notes"] = f"phase1 {p1} for inner_category"
                all_results.append(result)
                print(f"  [SKIP] phase1 {p1}")
                continue

            try:
                status, current_inner = phase2_create_value(page, inner, value, current_inner)
                result["status"] = status
            except Exception as e:
                print(f"  [ERROR] {e}")
                result["status"] = ERROR
                result["notes"] = str(e)
                current_inner = ""

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
    p1_counts: dict[str, int] = {}
    for s in phase1_status.values():
        p1_counts[s] = p1_counts.get(s, 0) + 1
    print(f"  Phase 1 (inner categories): {p1_counts}")
    vc = df_log["status"].value_counts().to_dict()
    print(f"  Phase 2 (values):           {vc}")

    failed = [r for r in all_results if r["status"] in (FAILED, ERROR)]
    print(f"\n  Rows with failures/errors: {len(failed)}/{len(all_results)}")
    if failed:
        print("\n  ── Failed / Error rows ───────────────────────────────")
        for r in failed:
            line = (f"    [{r['outer_category']} / {r['inner_category']} / "
                    f"{r['value']}]  {r['status']}")
            if r.get("notes"):
                line += f"  — {r['notes']}"
            print(line)
        print("  ─────────────────────────────────────────────────────")

    print("═════════════════════════════════════════════════════════")
    print("Done.")
    _stop_log()


if __name__ == "__main__":
    run()
