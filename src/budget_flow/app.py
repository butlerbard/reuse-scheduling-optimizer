"""雙擊啟動與開發用命令列的批次入口。"""

from __future__ import annotations

import argparse
from datetime import datetime
import math
from pathlib import Path
import sys
from threading import Timer

from .core import InputError, GroupResult, solve_group, validate_groups
from .io import SUPPORTED, read_budget_file, write_transfers_xlsx


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


def _move_input(source: Path, destination: Path, success: bool) -> None:
    try:
        target = _available_path(destination, source.name)
        source.rename(target)
        _print(f"  原檔已移到 input\\{'已完成' if success else '失敗'}")
    except OSError:
        _print("  [警告] 原檔正被其他程式開啟，無法移動；請關閉 Excel 後手動移動，以免下次重複計算")


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
    base = f"{source.stem}_流用結果_{stamp}.xlsx"
    for _ in range(10000):
        target = _available_path(output_dir, base)
        try:
            write_transfers_xlsx(target, rows, results)
            return target
        except FileExistsError:
            continue
    raise OSError("輸出檔名流水號已用盡")


def process_file(source: Path, output_dir: Path, time_limit: float,
                 archive_dir: Path | None = None,
                 failed_dir: Path | None = None) -> bool:
    """單檔失敗不拋出到批次外；開發用單檔模式可不移動原檔。"""
    try:
        rows = read_budget_file(source)
        groups = validate_groups(rows)
        _print(f"  讀取 {len(rows)} 個科目，分為 {len(groups)} 組")
        results = _solve_with_progress(rows, time_limit)
        target = _write_unique_output(source, output_dir, rows, results)
        _print(f"  [成功] 已輸出：{target.name}")
        if archive_dir is not None:
            _move_input(source, archive_dir, True)
        return True
    except InputError as error:
        _print("  [失敗] 未輸出結果檔，原因如下：")
        for line in str(error).splitlines():
            _print(f"  ・{line}")
    except Exception as error:
        _print("  [失敗] 發生未預期的錯誤，本檔案未輸出結果檔：")
        _print(f"         {error}")
        _print("         請將此畫面截圖給維護人員。")
    if failed_dir is not None:
        _move_input(source, failed_dir, False)
    return False


def run_batch(base: Path, time_limit: float = 30) -> tuple[int, int]:
    base = Path(base).resolve()
    input_dir = base / "input"
    output_dir = base / "output"
    done_dir = input_dir / "已完成"
    failed_dir = input_dir / "失敗"
    for folder in (input_dir, output_dir, done_dir, failed_dir):
        folder.mkdir(parents=True, exist_ok=True)
    files = sorted((path for path in input_dir.iterdir()
                    if path.is_file() and path.suffix.lower() in SUPPORTED
                    and not path.name.startswith("~$")), key=lambda path: path.name.casefold())
    _print("=" * 60)
    _print("  預算科目流用最佳解工具")
    _print("=" * 60)
    if not files:
        _print("input 資料夾中沒有可計算的檔案（.xlsx／.xls／.csv）。")
        _print("請把檔案放進 input 資料夾後再執行一次。")
        return 0, 0
    _print(f"input 資料夾中找到 {len(files)} 個檔案，開始計算……")
    succeeded = 0
    for index, source in enumerate(files, 1):
        _print()
        _print(f"[{index}/{len(files)}] {source.name}")
        if process_file(source, output_dir, time_limit, done_dir, failed_dir):
            succeeded += 1
    failed = len(files) - succeeded
    _print()
    _print("=" * 60)
    _print(f"完成：成功 {succeeded} 個、失敗 {failed} 個")
    _print("結果在 output 資料夾")
    _print("=" * 60)
    return succeeded, failed


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="預算科目流用最佳解工具")
    parser.add_argument("--base", type=Path, default=Path.cwd(), help="含 input／output 的工具資料夾")
    parser.add_argument("--file", type=Path, help="開發用：只處理指定檔案，不移動原檔")
    parser.add_argument("--time-limit", type=float, default=30, help="每組求解秒數上限")
    args = parser.parse_args(argv)
    if not math.isfinite(args.time_limit) or args.time_limit <= 0:
        parser.error("--time-limit 必須為大於 0 的有限數字")
    try:
        if args.file:
            output_dir = args.base.resolve() / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            return 0 if process_file(args.file, output_dir, args.time_limit) else 1
        _, failed = run_batch(args.base, args.time_limit)
        return 1 if failed else 0
    except Exception as error:
        _print(f"[失敗] 發生未預期的錯誤：{error}")
        _print("請將此畫面截圖給維護人員。")
        return 1
