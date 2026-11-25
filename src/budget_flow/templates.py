"""範本資料夾的防呆補建：缺少的範本依固定內容重建，既有檔案一律不覆蓋。"""

from __future__ import annotations

from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill

TEMPLATE_DIR = "範本"
XLSX_NAME = "流用預算表_範本.xlsx"
CSV_NAME = "流用預算表_範本.csv"
HEADERS = ("預算編號", "預算名稱", "原預算金額", "流入後金額", "差異")
LAST_ROW = 501

INSTRUCTIONS = (
    ("填寫說明（這一頁只是說明，不會被計算）", True),
    ("", False),
    ("1. 到第一頁「預算表」，從第 2 列開始，一列填一個科目。", False),
    ("2. 只要填前四欄：預算編號、預算名稱、原預算金額、流入後金額。", False),
    ("3. 「差異」欄會自動算好（流入後金額 減 原預算金額），不用填。負數代表錢要流出，正數代表錢要流入。", False),
    ("4. 預算編號照這個樣子寫：AZ0-115F001-008。中間剛好兩個「-」，同一個編號不可以出現兩次。", False),
    ("5. 金額填整數就好，不要有小數；原預算金額和流入後金額不可以是負數。", False),
    ("6. 編號前兩段相同的科目（例如 AZ0-115F001-001 和 AZ0-115F001-002）才能互相流用。", False),
    ("   這些科目的差異加起來要剛好是 0，也就是流出多少，流入就要多少。", False),
    ("7. 填好後按「儲存」，把這個檔案放在工具資料夾（和「啟動流用計算.bat」同一層），再雙擊「啟動流用計算.bat」。", False),
    ("", False),
    ("小提醒：", False),
    ("・科目超過 500 個時，把「差異」欄最後一格往下拉，複製到需要的列數即可。", False),
    ("・如果執行時出現「請用 Excel 開啟後存檔一次」：打開這個檔案，按一次「儲存」，再執行一次就好。", False),
)


def _write_xlsx(path: Path) -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "預算表"
    header_fill = PatternFill("solid", fgColor="EEE2CF")
    formula_fill = PatternFill("solid", fgColor="F6EFE3")
    for column, (title, width) in enumerate(zip(HEADERS, (20, 26, 14, 14, 14)), 1):
        cell = sheet.cell(1, column, title)
        cell.font = Font(bold=True)
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")
        sheet.column_dimensions[cell.column_letter].width = width
    for row in range(2, LAST_ROW + 1):
        sheet.cell(row, 1).number_format = "@"
        sheet.cell(row, 3).number_format = "#,##0"
        sheet.cell(row, 4).number_format = "#,##0"
        diff = sheet.cell(row, 5, f'=IF(AND(C{row}="",D{row}=""),"",D{row}-C{row})')
        diff.number_format = "#,##0;[Red]-#,##0"
        diff.fill = formula_fill
    sheet.freeze_panes = "A2"

    guide = workbook.create_sheet("填寫說明")
    guide.column_dimensions["A"].width = 100
    for row, (text, title) in enumerate(INSTRUCTIONS, 1):
        if text:
            guide.cell(row, 1, text).font = Font(bold=True, size=13) if title else Font(size=11)
    workbook.active = 0
    _save_new(path, workbook.save)


def _write_csv(path: Path) -> None:
    content = ",".join(HEADERS) + "\n"
    _save_new(path, lambda target: Path(target).write_text(content, encoding="utf-8-sig", newline=""))


def _save_new(path: Path, writer) -> None:
    """先寫到暫存檔再改名，避免中途失敗留下不完整的範本。"""
    temp = path.with_name(path.name + ".tmp")
    try:
        writer(temp)
        temp.replace(path)
    finally:
        if temp.exists():
            temp.unlink()


def ensure_templates(base: Path) -> list[str]:
    """回傳要顯示在畫面上的訊息；失敗只警告，不中斷計算。"""
    messages = []
    folder = Path(base) / TEMPLATE_DIR
    for name, writer in ((XLSX_NAME, _write_xlsx), (CSV_NAME, _write_csv)):
        target = folder / name
        if target.exists():
            continue
        try:
            folder.mkdir(parents=True, exist_ok=True)
            writer(target)
            messages.append(f"[提示] 找不到 {TEMPLATE_DIR}\\{name}，已重新建立")
        except Exception as error:
            messages.append(f"[警告] 無法重新建立 {TEMPLATE_DIR}\\{name}（{error}），計算照常進行")
    return messages
