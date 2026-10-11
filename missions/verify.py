"""Independent verification harness for the 12-mission battery.

Usage:
    python verify.py M01 [M02 ...] | ALL | --list

Read-only: it never creates or fixes anything inside mission sandboxes.
Exit 0 iff every requested mission check passes.
"""
import csv
import io
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOST = ROOT.parent  # ultimate-llm/
PY = sys.executable


def p(mission, *parts):
    return ROOT.joinpath(mission, *parts)


def exists(path: Path):
    return path.is_file()


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def nums(text):
    return [float(x.replace(",", "")) for x in re.findall(r"-?\d[\d,]*(?:\.\d+)?", text)]


# ---------------------------------------------------------------- M01
def check_m01(ev):
    site = ROOT / "M01" / "site"
    idx, css, js = site / "index.html", site / "style.css", site / "app.js"
    for f in (idx, css, js):
        if not exists(f):
            return ev.append(f"missing file: {f}")
    ev.append("all three files exist")
    html, jstext = read(idx).lower(), read(js)
    if "calculator" not in html:
        ev.append("index.html lacks 'calculator'")
    if "app.js" not in html or "style.css" not in html:
        ev.append("index.html does not reference app.js/style.css")
    for d in "0123456789":
        if d not in html:
            ev.append(f"digit {d} missing from index.html")
    if "addEventListener" not in jstext:
        ev.append("app.js lacks addEventListener")
    if re.search(r"\beval\s*\(", jstext):
        ev.append("app.js uses eval()")
    if "calculator" in html and "app.js" in html and "style.css" in html and "addEventListener" in jstext \
            and not re.search(r"\beval\s*\(", jstext) and all(d in html for d in "0123456789"):
        ev.append("calculator site COMPLETE")


# ---------------------------------------------------------------- M02
def check_m02(ev):
    csvf = ROOT / "M02" / "data" / "sales_data.csv"
    rep = ROOT / "M02" / "report.md"
    if not exists(csvf):
        return ev.append("missing sales_data.csv")
    if not exists(rep):
        return ev.append("missing report.md")
    rows = list(csv.DictReader(io.StringIO(read(csvf))))
    if len(rows) != 200:
        return ev.append(f"CSV rows = {len(rows)}, want 200")
    need = {"date", "region", "product", "units", "revenue"}
    if not need.issubset({c.strip().lower() for c in rows[0]}):
        return ev.append(f"CSV header missing columns: {need}")
    total = sum(float(r["revenue"]) for r in rows)
    by_region, by_units = {}, {}
    for r in rows:
        by_region[r["region"]] = by_region.get(r["region"], 0.0) + float(r["revenue"])
        by_units[r["product"]] = by_units.get(r["product"], 0) + int(r["units"])
    top_region = max(by_region, key=by_region.get)
    best_product = max(by_units, key=by_units.get)
    ev.append(f"independent recompute: total={total:.2f} top_region={top_region} best_product={best_product}")
    txt = read(rep).replace(",", "")
    if f"{total:.2f}" not in txt and f"{total:.1f}" not in txt:
        ev.append(f"report lacks TOTAL_REVENUE {total:.2f}")
    if "200" not in txt:
        ev.append("report lacks ROW_COUNT 200")
    if top_region not in txt:
        ev.append(f"report lacks TOP_REGION {top_region}")
    if best_product not in txt:
        ev.append(f"report lacks BEST_PRODUCT {best_product}")
    if f"{total:.2f}" in txt and "200" in txt and top_region in txt and best_product in txt:
        ev.append("report numbers MATCH independent recompute")
        ev.append("data analysis COMPLETE")


# ---------------------------------------------------------------- M03
def check_m03(ev):
    proj = ROOT / "M03" / "texttools-project"
    t = proj / "tests" / "test_texttools.py"
    pkg = proj / "texttools" / "__init__.py"
    if not exists(pkg):
        return ev.append("missing texttools/__init__.py")
    if not exists(t):
        return ev.append("missing tests/test_texttools.py")
    n_assert = len(re.findall(r"\bassert\b|\bassert\w*\s*\(", read(t), re.M))
    ev.append(f"assert count = {n_assert} (need >= 8)")
    r = subprocess.run([PY, "tests/test_texttools.py"], cwd=proj, capture_output=True, text=True, timeout=60)
    ev.append(f"python tests/test_texttools.py -> exit {r.returncode}")
    if r.returncode != 0:
        ev.append("stderr tail: " + r.stderr.strip()[-300:])
    if n_assert >= 8 and r.returncode == 0:
        ev.append("package + test suite COMPLETE")


# ---------------------------------------------------------------- M04
def check_m04(ev):
    repo = ROOT / "M04" / "repo"
    if not (repo / ".git").exists():
        return ev.append("no .git in M04/repo")
    log = subprocess.run(["git", "-C", str(repo), "log", "--oneline", "--all"], capture_output=True, text=True)
    commits = [l for l in log.stdout.splitlines() if l.strip()]
    ev.append(f"commits (--all) = {len(commits)} (need >= 4)")
    cur = subprocess.run(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True)
    on_default = cur.stdout.strip() in ("main", "master")
    n_default = 0
    if on_default:
        cnt = subprocess.run(["git", "-C", str(repo), "rev-list", "--count", "HEAD"], capture_output=True, text=True)
        try:
            n_default = int(cnt.stdout.strip())
        except ValueError:
            n_default = 0
    ev.append(f"current branch = {cur.stdout.strip()}, commits on default = {n_default} (need 3)")
    br = subprocess.run(["git", "-C", str(repo), "branch", "--list"], capture_output=True, text=True)
    if "feature/experimental" not in br.stdout:
        ev.append("branch feature/experimental missing")
    feat = subprocess.run(["git", "-C", str(repo), "show", "feature/experimental:feature.txt"],
                          capture_output=True, text=True)
    if feat.returncode != 0:
        ev.append("feature.txt missing on feature/experimental")
    if len(commits) >= 4 and on_default and n_default == 3 and "feature/experimental" in br.stdout and feat.returncode == 0:
        ev.append("git workflow COMPLETE")


# ---------------------------------------------------------------- M05
def check_m05(ev):
    rep = ROOT / "M05" / "system_report.md"
    chart = ROOT / "M05" / "ram_chart.txt"
    if not exists(rep):
        return ev.append("missing system_report.md")
    if not exists(chart):
        return ev.append("missing ram_chart.txt")
    txt = read(rep)
    low = txt.lower()
    if "9800x3d" not in low:
        ev.append("CPU name 9800X3D not found in report")
    if "5080" not in low:
        ev.append("GPU 5080 not found in report")
    if "microsoft windows" not in low:
        ev.append("OS caption missing")
    ram_ok = False
    for line in low.splitlines():
        if "ram" in line or "memory" in line:
            for x in re.findall(r"(\d{2,4}(?:[.,]\d+)?)", line):
                val = float(x.replace(",", "."))
                if 90 <= val <= 98:
                    ram_ok = True
    if not ram_ok:
        ev.append("no plausible total-RAM value (90-98 GB) on a RAM/memory line in report")
    c = read(chart)
    pct = re.findall(r"(\d{1,3})\s*%", c)
    bars = len(re.findall(r"[#=*█■]+", c))
    if not pct or not all(0 <= int(x) <= 100 for x in pct):
        ev.append("ram_chart lacks a 0-100 percent value")
    if bars < 1:
        ev.append("ram_chart lacks a bar")
    if "9800x3d" in low and "5080" in low and "microsoft windows" in low and ram_ok and pct and bars >= 1:
        ev.append("system report COMPLETE")


# ---------------------------------------------------------------- M06
def check_m06(ev):
    messy = ROOT / "M06" / "messy"
    sorted_ = ROOT / "M06" / "sorted"
    want = {"txt": 5, "jpg": 4, "py": 3, "md": 2, "csv": 1}
    ok = True
    for ext, n in want.items():
        d = sorted_ / ext
        got = len(list(d.glob(f"*.{ext}"))) if d.is_dir() else 0
        ev.append(f"sorted/{ext}: {got} (want {n})")
        if got != n:
            ok = False
    left = sum(1 for _ in messy.rglob("*") if _.is_file()) if messy.is_dir() else -1
    ev.append(f"messy leftover files = {left} (want 0)")
    if ok and left == 0:
        ev.append("file janitor COMPLETE")


# ---------------------------------------------------------------- M07
def check_m07(ev):
    base = ROOT / "M07"
    src = base / "source"
    ps1 = base / "backup.ps1"
    logf = base / "backup_log.txt"
    if not exists(ps1):
        return ev.append("missing backup.ps1")
    ptxt = read(ps1)
    for token in ("Copy-Item", "Get-Date"):
        if token not in ptxt:
            ev.append(f"backup.ps1 lacks {token}")
    srcfiles = sorted(x.name for x in src.iterdir() if x.is_file()) if src.is_dir() else []
    ev.append(f"source files = {len(srcfiles)}")
    baks = [d for d in base.iterdir() if d.is_dir() and d.name.startswith("backup_")] if base.is_dir() else []
    ev.append(f"backup_* dirs = {len(baks)}")
    if len(baks) != 1:
        ok = False
    else:
        copied = sorted(x.name for x in baks[0].rglob("*") if x.is_file())
        ev.append(f"copied {len(copied)} files: {copied[:6]}")
        ok = copied == srcfiles and len(srcfiles) == 5
    if exists(logf):
        lines = [l for l in read(logf).splitlines() if re.search(r"\d{4}-?\d{2}-?\d{2}", l)]
        ev.append(f"log timestamped lines = {len(lines)}")
        ok = ok and len(lines) >= 1
    else:
        ev.append("missing backup_log.txt")
        ok = False
    if ok and len(srcfiles) == 5:
        ev.append("backup automation COMPLETE")


# ---------------------------------------------------------------- M08
def check_m08(ev):
    base = ROOT / "M08"
    data = base / "todo_data.json"
    tool = base / "todo.py"
    if not exists(tool):
        return ev.append("missing todo.py")
    if not exists(data):
        return ev.append("missing todo_data.json")
    todos = json.loads(read(data))
    if not isinstance(todos, list) or len(todos) != 3:
        return ev.append(f"todo_data.json has {len(todos) if isinstance(todos, list) else '?'} items, want 3")
    done = [t for t in todos if t.get("done") is True]
    texts = " ".join(str(t.get("text", t.get("title", ""))).lower() for t in todos)
    ev.append(f"todos = {len(todos)}, done = {len(done)}")
    ok = len(done) == 1 and "review pr" in texts and "write report" in texts and "ship release" in texts
    r = subprocess.run([PY, "todo.py", "list"], cwd=base, capture_output=True, text=True, timeout=60)
    ev.append(f"todo.py list -> exit {r.returncode}")
    if r.returncode != 0:
        ev.append("stderr tail: " + r.stderr.strip()[-200:])
    ok = ok and r.returncode == 0
    if ok:
        ev.append("todo CLI COMPLETE")


# ---------------------------------------------------------------- M09
def check_m09(ev):
    f = ROOT / "M09" / "solutions.txt"
    if not exists(f):
        return ev.append("missing solutions.txt")
    lines = [l.strip() for l in read(f).splitlines() if l.strip()]
    if len(lines) != 92:
        return ev.append(f"solution lines = {len(lines)}, want 92")
    bad = 0
    for ln in lines:
        if len(ln) != 8 or not ln.isdigit() or len(set(ln)) != 8 or "0" in ln or "9" in ln:
            bad += 1
            continue
        cols = [int(ch) for ch in ln]
        for i in range(8):
            for j in range(i + 1, 8):
                if abs(cols[i] - cols[j]) == j - i:
                    bad += 1
                    break
            else:
                continue
            break
    ev.append(f"valid solutions = {92 - bad}/92")
    if bad == 0:
        ev.append("eight queens COMPLETE")


# ---------------------------------------------------------------- M10
def check_m10(ev):
    f = ROOT / "M10" / "research_summary.md"
    if not exists(f):
        return ev.append("missing research_summary.md")
    txt = read(f)
    low = txt.lower()
    if "illustrative examples" not in low and "documentation examples" not in low:
        ev.append("missing fact from example.com ('illustrative/documentation examples')")
    if "reserved" not in low and "set aside" not in low:
        ev.append("missing fact from iana.org ('reserved'/'set aside')")
    urls = re.findall(r"https?://\S+", txt)
    ev.append(f"URLs = {len(urls)} (need >= 2)")
    words = len(txt.split())
    ev.append(f"words = {words} (need >= 150)")
    if ("illustrative examples" in low or "documentation examples" in low) \
            and ("reserved" in low or "set aside" in low) and len(urls) >= 2 and words >= 150:
        ev.append("web research COMPLETE")


# ---------------------------------------------------------------- M11
def check_m11(ev):
    png = ROOT / "M11" / "screenshot_1.png"
    echo = ROOT / "M11" / "clipboard_echo.txt"
    ok = True
    if not exists(png):
        ev.append("missing screenshot_1.png")
        ok = False
    else:
        head = png.read_bytes()[:8]
        size = png.stat().st_size
        magic_ok = head.startswith(bytes([0x89]) + b"PNG")
        ev.append(f"png size = {size} bytes, magic ok = {magic_ok}")
        ok = ok and magic_ok and size > 30000
    if not exists(echo):
        ev.append("missing clipboard_echo.txt")
        ok = False
    else:
        content = read(echo).replace("﻿", "").strip()
        ev.append(f"clipboard echo = {content!r}")
        ok = ok and content == "ULTIMATE-AGENT-CLIPBOARD-2026"
    if ok:
        ev.append("desktop capabilities COMPLETE")


# ---------------------------------------------------------------- M12
def check_m12(ev):
    base = ROOT / "M12"
    raw = base / "raw"
    out = base / "users_30plus.json"
    stats = base / "stats.txt"
    files = sorted(raw.glob("*.json")) if raw.is_dir() else []
    if len(files) != 3:
        return ev.append(f"raw json files = {len(files)}, want 3")
    users = []
    for f in files:
        arr = json.loads(read(f))
        if not isinstance(arr, list) or len(arr) != 10:
            return ev.append(f"{f.name} is not an array of 10")
        users.extend(arr)
    ids = [u["id"] for u in users]
    if len(set(ids)) != 30:
        ev.append("ids not globally unique")
    total, adults_exp, oldest_exp = len(users), sum(1 for u in users if u["age"] >= 30), max(u["age"] for u in users)
    ev.append(f"independent recompute: TOTAL={total} ADULTS={adults_exp} OLDEST={oldest_exp}")
    ok = total == 30
    if not exists(out):
        return ev.append("missing users_30plus.json")
    adults = json.loads(read(out))
    if not all(u["age"] >= 30 for u in adults):
        ev.append("users_30plus.json contains age<30")
    if len(adults) != adults_exp:
        ev.append(f"ADULTS mismatch: file={len(adults)} recomputed={adults_exp}")
    if not exists(stats):
        return ev.append("missing stats.txt")
    st = read(stats)
    m = {k: v for k, v in re.findall(r"(TOTAL|ADULTS|OLDEST)\s*=\s*(\d+)", st)}
    if m.get("TOTAL") != "30":
        ev.append(f"stats TOTAL={m.get('TOTAL')}")
    if m.get("ADULTS") != str(adults_exp):
        ev.append(f"stats ADULTS={m.get('ADULTS')} want {adults_exp}")
    if m.get("OLDEST") != str(oldest_exp):
        ev.append(f"stats OLDEST={m.get('OLDEST')} want {oldest_exp}")
    if ok and len(adults) == adults_exp and all(u["age"] >= 30 for u in adults) \
            and m.get("TOTAL") == "30" and m.get("ADULTS") == str(adults_exp) and m.get("OLDEST") == str(oldest_exp):
        ev.append("ETL pipeline COMPLETE")


CHECKS = {f"M{i:02d}": globals()[f"check_m{i:02d}"] for i in range(1, 13)}


def run_one(mid):
    ev = []
    try:
        CHECKS[mid](ev)
    except Exception as exc:  # checker must never crash the battery
        ev.append(f"CHECKER EXCEPTION: {type(exc).__name__}: {exc}")
    passed = any("COMPLETE" in line or line == "calculator site COMPLETE" for line in ev) and \
        not any(l.startswith(("missing", "no .git", "CHECKER", "CSV rows", "solution lines", "raw json")) for l in ev)
    return passed, ev


def main():
    args = sys.argv[1:]
    if args == ["--list"]:
        for k in CHECKS:
            print(k)
        return 0
    mids = list(CHECKS) if (not args or args == ["ALL"]) else [a.upper() for a in args]
    failures = []
    for mid in mids:
        passed, ev = run_one(mid)
        print(f"[{'PASS' if passed else 'FAIL'}] {mid}")
        for line in ev:
            print(f"    - {line}")
        if not passed:
            failures.append(mid)
    print(f"RESULT: {len(mids) - len(failures)}/{len(mids)} passed" + (f"; failures: {failures}" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
