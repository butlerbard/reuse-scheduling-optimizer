"""雙擊啟動與開發用命令列的批次入口。"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime
import math
import os
from pathlib import Path
import sys
from threading import Timer

from .core import InputError, GroupResult, solve_group, validate_groups
from .io import SUPPORTED, read_budget_file, write_transfers_xlsx
from .templates import ensure_templates

RESULT_MARK = "_流用結果_"


def _print(message: str = "") -> None:
    print(message, flush=True)


def _available_path(folder: Path, name: str) -> Path:
    candidate = folder / name
    if not candidate.exists():
        return candidate
    stem, suffix = Path(name).stem, Path(name).suffix
    number = 1
    while True:
        candidate = folder / f"{stem}_{number}{suffix}"
        if not candidate.exists():
            return candidate
        number += 1


class _SHFileOpStruct(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]


_FO_DELETE = 3
# FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI | FOF_WANTNUKEWARNING
# 最後一項：磁碟沒有資源回收筒時會先跳出確認，不會悄悄永久刪除。
_RECYCLE_FLAGS = 0x0004 | 0x0010 | 0x0040 | 0x0400 | 0x4000


def _send_to_recycle_bin(path: Path) -> bool:
    """移到 Windows 資源回收筒；檔案被其他程式開啟等原因失敗時回傳 False，原檔保留。"""
    path = Path(path).resolve()
    if os.name != "nt" or not path.is_file():
        return False
    # pFrom 必須以兩個空字元結尾；ctypes 會自動補上第二個。
    operation = _SHFileOpStruct(None, _FO_DELETE, str(path) + "\0", None,
                                _RECYCLE_FLAGS, False, None, None)
    try:
        code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    except OSError:
        return False
    return code == 0 and not operation.fAnyOperationsAborted and not path.exists()


def _recycle_source(source: Path) -> None:
    if _send_to_recycle_bin(source):
        _print("  原檔已移到資源回收筒")
    else:
        _print("  [警告] 原檔正被其他程式開啟，無法移到資源回收筒，")
        _print("         請關閉 Excel 後自行刪除，以免下次重複計算")


def _supported_files(folder: Path) -> list[Path]:
    """資料夾第一層的 .xlsx／.xls／.csv，略過 ~$ 暫存檔；不看子資料夾。"""
    return sorted((path for path in folder.iterdir()
                   if path.is_file() and path.suffix.lower() in SUPPORTED
                   and not path.name.startswith("~$")), key=lambda path: path.name.casefold())


def _ask_yes_no(question: str) -> bool | None:
    """只接受 Y／N；其他輸入（含直接按 Enter）重新詢問。沒有可讀的輸入時回傳 None。"""
    while True:
        _print(question)
        try:
            answer = input().strip()
        except EOFError:
            return None
        if answer in ("Y", "y"):
            return True
        if answer in ("N", "n"):
            return False
        _print("請輸入 Y 或 N。")


def _handle_previous_results(base: Path) -> None:
    """先前的結果檔只用檔名辨識來詢問；未移走的照常列入計算。"""
    previous = [path for path in _supported_files(base) if RESULT_MARK in path.name]
    if not previous:
        return
    _print(f"發現 {len(previous)} 個先前的結果檔：")
    for path in previous:
        _print(f"  {path.name}")
    if _ask_yes_no("是否移到資源回收筒？（不移走會被當成新檔案重新計算）[Y/N]"):
        for path in previous:
            if _send_to_recycle_bin(path):
                _print(f"已移到資源回收筒：{path.name}")
            else:
                _print(f"[警告] {path.name} 正被其他程式開啟，無法移動，")
                _print("       本次會當成新檔案計算")
    _print()


def _open_results(paths: list[Path]) -> int:
    opened = 0
    for path in paths:
        try:
            os.startfile(path)
            opened += 1
        except (OSError, AttributeError):
            _print(f"[警告] 無法自動開啟 {path.name}，")
            _print("       請到工具資料夾手動開啟")
    return opened


def _explanation(result: GroupResult) -> str:
    if result.edge_status != "已證明最少":
        return f"轉移筆數未能證明是最少（{result.edge_status}）；拆分科目數也未證明。"
    return f"已找到最少轉移筆數；拆分科目數未能證明是最少（{result.split_status}）。"


def _solve_with_progress(rows, time_limit: float) -> list[GroupResult]:
    results = []
    for group, members in validate_groups(rows).items():
        timer = Timer(2, _print,
                      args=(f"  ・{group}：計算中，最多約 {time_limit:g} 秒……",))
        timer.daemon = True
        timer.start()
        try:
            result = solve_group(group, members, time_limit)
        finally:
            timer.cancel()
        results.append(result)
        status = "最佳解" if result.optimal else "可行方案"
        _print(f"  ・{group}：{result.transfer_count} 筆轉移、"
               f"{result.split_count} 個拆分科目（{status}）")
        if not result.optimal:
            _print(f"      說明：{_explanation(result)}")
    return results


def _write_unique_output(source: Path, output_dir: Path, rows,
                         results: list[GroupResult]) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"{source.stem}{RESULT_MARK}{stamp}.xlsx"
    for _ in range(10000):
        target = _available_path(output_dir, base)
        try:
            write_transfers_xlsx(target, rows, results)
            return target
        except FileExistsError:
            continue
    raise OSError("輸出檔名流水號已用盡")


def process_file(source: Path, output_dir: Path, time_limit: float,
                 recycle_source: bool = False) -> Path | None:
    """成功時回傳結果檔路徑；單檔失敗不拋出到批次外。開發用單檔模式不移動原檔。"""
    try:
        rows = read_budget_file(source)
        groups = validate_groups(rows)
        _print(f"  讀取 {len(rows)} 個科目，分為 {len(groups)} 組")
        results = _solve_with_progress(rows, time_limit)
        target = _write_unique_output(source, output_dir, rows, results)
        _print(f"  [成功] 已輸出：{target.name}")
        if recycle_source:
            _recycle_source(source)
        return target
    except InputError as error:
        _print("  [失敗] 未輸出結果檔，原因如下：")
        for line in str(error).splitlines():
            _print(f"  ・{line}")
        if recycle_source:
            _print("  原檔保留在工具資料夾，請修正後再執行一次")
    except Exception as error:
        _print("  [失敗] 發生未預期的錯誤，原檔保留在工具資料夾：")
        _print(f"         {error}")
        _print("         請將此畫面截圖給維護人員。")
    return None


def run_batch(base: Path, time_limit: float = 30) -> tuple[int, int]:
    """使用者簡易操作特規：讀工具根目錄第一層的檔案，結果寫回根目錄。"""
    base = Path(base).resolve()
    _print("=" * 60)
    _print("  預算科目流用最佳解工具")
    _print("=" * 60)
    for message in ensure_templates(base):
        _print(message)
    _handle_previous_results(base)
    files = _supported_files(base)
    if not files:
        _print("工具資料夾中沒有可計算的檔案（.xlsx／.xls／.csv）。")
        _print("請把檔案放在「啟動流用計算.bat」旁邊後再執行一次。")
        return 0, 0
    _print(f"工具資料夾中找到 {len(files)} 個檔案，開始計算……")
    outputs = []
    for index, source in enumerate(files, 1):
        _print()
        _print(f"[{index}/{len(files)}] {source.name}")
        target = process_file(source, base, time_limit, recycle_source=True)
        if target is not None:
            outputs.append(target)
    succeeded = len(outputs)
    failed = len(files) - succeeded
    _print()
    _print("=" * 60)
    _print(f"完成：成功 {succeeded} 個、失敗 {failed} 個")
    if outputs:
        opened = _open_results(outputs)
        if opened:
            _print(f"已開啟結果檔 {opened} 個（在工具資料夾）")
        _print("提醒：成功的原始檔已移到資源回收筒；")
        _print("      結果檔看完請移到其他資料夾或刪除；")
        _print("      留在工具資料夾的話，下次啟動會詢問是否移到資源回收筒")
    _print("=" * 60)
    return succeeded, failed


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="預算科目流用最佳解工具")
    parser.add_argument("--base", type=Path, default=Path.cwd(), help="工具資料夾（讀取與輸出的根目錄）")
    parser.add_argument("--file", type=Path, help="開發用：只處理指定檔案，不移動原檔、不開啟結果")
    parser.add_argument("--time-limit", type=float, default=30, help="每組求解秒數上限")
    args = parser.parse_args(argv)
    if not math.isfinite(args.time_limit) or args.time_limit <= 0:
        parser.error("--time-limit 必須為大於 0 的有限數字")
    try:
        if args.file:
            target = process_file(args.file, args.base.resolve(), args.time_limit)
            return 0 if target is not None else 1
        _, failed = run_batch(args.base, args.time_limit)
        return 1 if failed else 0
    except Exception as error:
        _print(f"[失敗] 發生未預期的錯誤：{error}")
        _print("請將此畫面截圖給維護人員。")
        return 1
