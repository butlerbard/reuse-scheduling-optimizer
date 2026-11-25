"""Excel／CSV 輸入驗證與分頁 Excel 輸出（原始資料、總覽、各組別）。"""

from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation
from io import StringIO
import math
from pathlib import Path
import re
from typing import Iterator
from zipfile import BadZipFile

import openpyxl
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
import xlrd

from .core import BudgetRow, GroupResult, InputError, validate_groups, verify_result


HEADERS = ("預算編號", "預算名稱", "原預算金額", "流入後金額", "差異")
OUTPUT_HEADERS = ("流出預算編號", "流出預算名稱", "流入預算編號", "流入預算名稱", "金額")
NUMBER = re.compile(r"-?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?")
SUPPORTED = {".xlsx", ".xls", ".csv"}


def _header_indices(header: tuple[object, ...]) -> dict[str, int]:
    normalized = [str(value).strip().lstrip("*").strip() if value is not None else ""
                  for value in header]
    errors = []
    indices = {}
    for field in HEADERS:
        matches = [i for i, name in enumerate(normalized) if name == field]
        if not matches:
            errors.append(f"缺少欄位「{field}」")
        elif len(matches) > 1:
            errors.append(f"欄位「{field}」重複")
        else:
            indices[field] = matches[0]
    if errors:
        raise InputError("第 1 列：" + "；".join(errors))
    return indices


def _amount(value: object, field: str, line: int, formula: bool = False) -> int:
    if value is None or (isinstance(value, str) and not value.strip()):
        if formula:
            raise InputError(f"第 {line} 列：「{field}」的公式還沒算出數字，請用 Excel 打開這個檔案、按一次儲存，再執行一次")
        raise InputError(f"第 {line} 列：「{field}」不可空白")
    if isinstance(value, bool):
        raise InputError(f"第 {line} 列：「{field}」不是整數元：{value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer():
            return int(value)
        raise InputError(f"第 {line} 列：「{field}」不是整數元：{value!r}")
    if isinstance(value, str):
        text = value.strip()
        if NUMBER.fullmatch(text):
            try:
                number = Decimal(text.replace(",", ""))
                if number == number.to_integral_value():
                    return int(number)
            except (InvalidOperation, OverflowError):
                pass
    raise InputError(f"第 {line} 列：「{field}」不是整數元：{value!r}")


def _csv_rows(path: Path) -> Iterator[tuple[int, tuple[object, ...], set[int]]]:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("cp950")
        except UnicodeDecodeError as error:
            raise InputError("CSV 不是 UTF-8 或 Big5（cp950）編碼") from error
    reader = csv.reader(StringIO(text, newline=""))
    for values in reader:
        yield reader.line_num, tuple(values), set()


def _xlsx_rows(path: Path) -> Iterator[tuple[int, tuple[object, ...], set[int]]]:
    cached = openpyxl.load_workbook(path, read_only=True, data_only=True)
    formulas = openpyxl.load_workbook(path, read_only=True, data_only=False)
    try:
        first = cached.worksheets[0]
        source = formulas.worksheets[0]
        for line, (values, originals) in enumerate(
            zip(first.iter_rows(values_only=True), source.iter_rows(values_only=True)), 1
        ):
            formula_columns = {i for i, value in enumerate(originals)
                               if isinstance(value, str) and value.startswith("=")}
            yield line, tuple(values), formula_columns
    finally:
        cached.close()
        formulas.close()


def _xls_rows(path: Path) -> Iterator[tuple[int, tuple[object, ...], set[int]]]:
    book = xlrd.open_workbook(path)
    sheet = book.sheet_by_index(0)
    for line in range(1, sheet.nrows + 1):
        yield line, tuple(sheet.row_values(line - 1)), set()


def read_budget_file(path: Path) -> list[BudgetRow]:
    """讀取一個檔案，集中回報可定位的資料錯誤。"""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED:
        raise InputError(f"不支援的檔案格式：{path.suffix}")
    try:
        iterator = {".csv": _csv_rows, ".xlsx": _xlsx_rows, ".xls": _xls_rows}[suffix](path)
        first_line, header, _ = next(iterator)
        if first_line != 1:
            raise InputError("找不到第 1 列欄位名稱")
        indices = _header_indices(header)
        rows = []
        errors = []
        codes: dict[str, list[int]] = {}
        for line, values, formula_columns in iterator:
            selected = {name: values[index] if index < len(values) else None
                        for name, index in indices.items()}
            if all(value is None or str(value).strip() == "" for value in selected.values()):
                continue
            for field in HEADERS[:2]:
                if selected[field] is None or not str(selected[field]).strip():
                    errors.append(f"第 {line} 列：「{field}」不可空白")
            code = str(selected["預算編號"]).strip() if selected["預算編號"] is not None else ""
            name = str(selected["預算名稱"]).strip() if selected["預算名稱"] is not None else ""
            if code:
                codes.setdefault(code, []).append(line)
                parts = code.split("-")
                if len(parts) != 3 or any(not part for part in parts):
                    errors.append(f"第 {line} 列：預算編號 {code} 格式錯誤，應恰有兩個 - 且三段非空")
            amounts = {}
            for field in HEADERS[2:]:
                try:
                    amounts[field] = _amount(selected[field], field, line,
                                             indices[field] in formula_columns)
                except InputError as error:
                    errors.append(str(error))
            if len(amounts) != 3 or not code or not name or len(code.split("-")) != 3:
                continue
            before, after, delta = (amounts[field] for field in HEADERS[2:])
            if before < 0 or after < 0:
                errors.append(f"第 {line} 列：原預算金額與流入後金額不可為負數")
                continue
            if after - before != delta:
                errors.append(f"第 {line} 列：差異應為 {after - before:+,}（流入後金額 − 原預算金額），表內為 {delta:+,}")
                continue
            rows.append(BudgetRow(line, code, name, before, after, delta))
        for code, lines in codes.items():
            if len(lines) > 1:
                errors.append(f"預算編號 {code} 重複，列號：{', '.join(str(line) for line in lines)}")
        if not rows and not errors:
            errors.append("沒有可計算的科目資料")
        if not errors:
            try:
                validate_groups(rows)
            except InputError as error:
                errors.append(str(error))
        if errors:
            raise InputError("\n".join(errors))
        return rows
    except StopIteration as error:
        raise InputError("檔案是空的，找不到第 1 列欄位名稱") from error
    except (OSError, ValueError, BadZipFile, csv.Error, xlrd.XLRDError) as error:
        if isinstance(error, InputError):
            raise
        raise InputError(f"無法讀取 {path.name}：{error}") from error


SHEET_FORBIDDEN = re.compile(r"[\[\]:*?/\\]")
ORIGINAL_SHEET = "原始資料"
OVERVIEW_SHEET = "排列後總覽"
OVERVIEW_HEADERS = ("流用組別",) + OUTPUT_HEADERS


def _fill_sheet(sheet, headers: tuple[str, ...], records, widths: tuple[int, ...],
                amount_columns: tuple[int, ...]) -> None:
    """寫入標題與資料，標題粗體並凍結，金額欄顯示千分位。"""
    sheet.append(headers)
    for record in records:
        sheet.append(record)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for index, width in enumerate(widths, 1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    for column in amount_columns:
        for (cell,) in sheet.iter_rows(min_row=2, min_col=column, max_col=column):
            cell.number_format = "#,##0"
    sheet.freeze_panes = "A2"


def _sheet_title(group: str, used: set[str]) -> str:
    """Excel 分頁名稱最多 31 字、不可含 []:*?/ 與反斜線，且不分大小寫不可重複。"""
    base = SHEET_FORBIDDEN.sub("_", group).strip("'")[:31] or "組別"
    title, number = base, 2
    while title.casefold() in used:
        suffix = f"_{number}"
        title = base[:31 - len(suffix)] + suffix
        number += 1
    used.add(title.casefold())
    return title


def write_transfers_xlsx(path: Path, rows: list[BudgetRow],
                         results: list[GroupResult]) -> None:
    """第 1 頁原始資料、第 2 頁全部轉移總覽，第 3 頁起每個流用組別一頁。

    用排他建立模式輸出，既有檔案絕不覆蓋。
    """
    for result in results:
        verify_result(result)
    book = openpyxl.Workbook()
    book.remove(book.active)
    used = {ORIGINAL_SHEET.casefold(), OVERVIEW_SHEET.casefold()}

    original = ((row.code, row.name, row.before, row.after, row.delta)
                for row in sorted(rows, key=lambda item: item.line))
    _fill_sheet(book.create_sheet(ORIGINAL_SHEET), HEADERS, original,
                (20, 26, 14, 14, 14), (3, 4, 5))

    ordered = sorted(results, key=lambda item: item.group)
    def transfers_of(result):
        return sorted(result.transfers,
                      key=lambda item: (item.source.code, item.target.code))
    overview = ((result.group, item.source.code, item.source.name,
                 item.target.code, item.target.name, item.amount)
                for result in ordered for item in transfers_of(result))
    _fill_sheet(book.create_sheet(OVERVIEW_SHEET), OVERVIEW_HEADERS, overview,
                (16, 20, 26, 20, 26, 14), (6,))

    for result in ordered:
        records = ((item.source.code, item.source.name,
                    item.target.code, item.target.name, item.amount)
                   for item in transfers_of(result))
        _fill_sheet(book.create_sheet(_sheet_title(result.group, used)),
                    OUTPUT_HEADERS, records, (20, 26, 20, 26, 14), (5,))

    target = Path(path)
    with target.open("xb") as stream:
        try:
            book.save(stream)
        except BaseException:
            stream.close()
            target.unlink(missing_ok=True)
            raise
