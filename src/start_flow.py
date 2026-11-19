"""雙擊啟動用：檢查 Python、建立 .venv、強制安裝必要套件，再執行流用計算。

由「啟動流用計算.bat」呼叫，只使用標準函式庫，並避免新版語法，
讓較舊的 Python 也能執行到版本檢查並顯示清楚訊息。
回傳碼 3 代表 Python 版本不足，.bat 會據此詢問是否安裝 Python。
"""

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
VENV_PYTHON = VENV / "Scripts" / "python.exe"
REQUIREMENTS = ROOT / "src" / "requirements.txt"
MARKER = VENV / "budget_flow_requirements.sha256"
MIN_VERSION = (3, 11)
NEED_PYTHON = 3
IMPORT_CHECK = "import scipy.optimize, openpyxl, xlrd"


def say(message=""):
    print(message, flush=True)


def run(command, quiet=False, env=None):
    output = subprocess.DEVNULL if quiet else None
    try:
        return subprocess.run([str(part) for part in command], cwd=str(ROOT),
                              stdout=output, stderr=output, env=env).returncode
    except OSError:
        return 1


def venv_usable():
    check = "import sys; sys.exit(0 if sys.version_info >= %r else 1)" % (MIN_VERSION,)
    return VENV_PYTHON.exists() and run([VENV_PYTHON, "-c", check], quiet=True) == 0


def requirements_digest():
    return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()


def packages_ready(digest):
    try:
        recorded = MARKER.read_text(encoding="ascii").strip()
    except OSError:
        return False
    return recorded == digest and run([VENV_PYTHON, "-c", IMPORT_CHECK], quiet=True) == 0


def prepare_environment():
    """回傳 True 表示 .venv 與套件都可使用。"""
    if VENV.exists() and not venv_usable():
        say("本工具的專用環境（.venv）無法使用，可能是 Python 被移除或更新，正在重新建立……")
        try:
            shutil.rmtree(VENV)
        except OSError as error:
            say("[失敗] 無法刪除舊的 .venv：%s" % error)
            say("請關閉其他使用本工具的視窗後再試；仍失敗時，請手動刪除工具資料夾中的 .venv 資料夾。")
            return False

    if not VENV_PYTHON.exists():
        say("首次設定：正在建立本工具專用的 Python 環境（.venv）……")
        if run([sys.executable, "-m", "venv", VENV]) != 0 or not VENV_PYTHON.exists():
            say("[失敗] 建立 .venv 失敗，尚未安裝任何套件，也未開始計算。")
            return False

    digest = requirements_digest()
    if packages_ready(digest):
        return True

    say("正在安裝必要套件（首次約需 1～3 分鐘，需要網路）……")
    code = run([VENV_PYTHON, "-m", "pip", "install", "--disable-pip-version-check",
                "--no-input", "-r", REQUIREMENTS])
    if code != 0 or run([VENV_PYTHON, "-c", IMPORT_CHECK], quiet=True) != 0:
        say("")
        say("[失敗] 必要套件安裝未完成，無法開始計算。常見原因：")
        say("  ・電腦沒有連上網路，或公司網路阻擋下載（請資訊人員協助開放 pypi.org 與 files.pythonhosted.org）")
        say("  ・防毒軟體攔截安裝")
        say("排除後重新雙擊「啟動流用計算.bat」即可，會自動重試。")
        return False
    try:
        MARKER.write_text(digest, encoding="ascii")
    except OSError:
        pass  # 只影響下次是否重新檢查安裝，不影響本次計算
    say("必要套件已就緒。")
    say("")
    return True


def main():
    if sys.version_info < MIN_VERSION:
        say("目前的 Python 版本是 %d.%d，本工具需要 %d.%d 以上。"
            % (sys.version_info[0], sys.version_info[1], MIN_VERSION[0], MIN_VERSION[1]))
        return NEED_PYTHON
    if not REQUIREMENTS.exists() or not (ROOT / "src" / "budget_flow").is_dir():
        say("[失敗] 找不到程式檔案，請重新下載完整的工具資料夾。")
        return 1
    if not prepare_environment():
        return 1

    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    env["PYTHONUTF8"] = "1"
    return run([VENV_PYTHON, "-m", "budget_flow", "--base", ROOT], env=env)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
    except Exception as error:  # 啟動程序本身的意外錯誤也要讓使用者看得到
        say("[失敗] 啟動程序發生未預期的錯誤：%s" % error)
        say("請將此畫面截圖給維護人員。")
        sys.exit(1)
