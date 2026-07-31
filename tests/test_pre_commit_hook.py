# -*- coding: utf-8 -*-
"""驗證 .githooks/pre-commit 真的能擋下 rentals_backup.json。

    python tests/test_pre_commit_hook.py

在系統暫存區建一個拋棄式 git repo 做真實 commit，不會動到本專案的 git 狀態。
"""
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK_SRC = os.path.join(ROOT, ".githooks", "pre-commit")
REPO = os.path.join(tempfile.gettempdir(), "ourhome_hooktest")

failures = []


def check(label, cond, extra=""):
    print(("[OK]   " if cond else "[FAIL] ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        failures.append(label)


def git(*args, env=None):
    return subprocess.run(["git"] + list(args), cwd=REPO, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", env=env)


def write(name, text):
    with open(os.path.join(REPO, name), "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


if not os.path.exists(HOOK_SRC):
    print(f"[FAIL] 找不到 hook: {HOOK_SRC}")
    sys.exit(1)

if os.path.exists(REPO):
    shutil.rmtree(REPO)
os.makedirs(os.path.join(REPO, ".githooks"))

git("init", "-q")
git("config", "user.email", "t@t")
git("config", "user.name", "t")
shutil.copy(HOOK_SRC, os.path.join(REPO, ".githooks", "pre-commit"))
git("config", "core.hooksPath", ".githooks")

write("rentals_backup.json", "cloud data v1")
write("app.py", "print('v1')")
git("add", "-A")
git("commit", "-q", "-m", "initial")
print("基準 commit 建立完成")

# ── 情境 1：git add . 把程式碼與資料檔一起帶進來 ──
print("\n── 情境1：`git add .` 之後 commit ──")
write("rentals_backup.json", "stale local data v2")
write("app.py", "print('v2')")
git("add", ".")
git("commit", "-m", "改程式碼，順手 add .")

files = [f for f in git("show", "--name-only", "--format=", "HEAD").stdout.split("\n") if f.strip()]
check("app.py 有進 commit", "app.py" in files, str(files))
check("rentals_backup.json 被擋在 commit 外", "rentals_backup.json" not in files)
check("repo 裡的資料檔仍是雲端版",
      git("show", "HEAD:rentals_backup.json").stdout.strip() == "cloud data v1")
with open(os.path.join(REPO, "rentals_backup.json"), encoding="utf-8") as f:
    check("工作目錄的檔案內容未被更動", f.read().strip() == "stale local data v2")

# ── 情境 2：逃生門 ──
print("\n── 情境2：OURHOME_ALLOW_BACKUP_COMMIT=1 ──")
git("add", ".")
git("commit", "-m", "刻意還原資料", env=dict(os.environ, OURHOME_ALLOW_BACKUP_COMMIT="1"))
check("設了逃生門就能提交資料檔",
      git("show", "HEAD:rentals_backup.json").stdout.strip() == "stale local data v2")

# ── 情境 3：只改程式碼，hook 不該干擾 ──
print("\n── 情境3：只改程式碼 ──")
write("app.py", "print('v3')")
git("add", "app.py")
r = git("commit", "-m", "只改 code")
check("一般 commit 不受影響",
      r.returncode == 0 and "app.py" in git("show", "--name-only", "--format=", "HEAD").stdout)

print("\n" + ("Hook 測試全部通過 ✅" if not failures else f"失敗 {len(failures)} 項 ❌: {failures}"))
sys.exit(1 if failures else 0)
