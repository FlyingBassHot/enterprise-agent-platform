"""Transaction spreadsheet parsing for the audit domain pack.

Pure module: no database imports. Accepts raw ``.xlsx``/``.xlsm``/``.csv``
bytes and returns normalized transactions plus parse-quality findings.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from openpyxl import load_workbook
from openpyxl.utils.datetime import from_excel

from app.core.exceptions import ValidationError

SUPPORTED_SUFFIXES = (".xlsx", ".xlsm", ".csv")
SUPPORTED_MIME_SUFFIX = {".xlsx", ".xlsm"}
MAX_ROWS = 50_000
MAX_COLUMNS = 200
MAX_PARSE_BYTES = 50 * 1024 * 1024

REQUIRED_FIELDS: tuple[str, ...] = ("date", "vendor", "amount", "payment_id")

FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "date": (
        "交易日期",
        "日期",
        "交易时间",
        "付款日期",
        "date",
        "transaction_date",
        "payment_date",
    ),
    "vendor": (
        "供应商",
        "供应商名称",
        "收款方",
        "收款单位",
        "vendor",
        "supplier",
        "vendor_name",
    ),
    "vendor_code": (
        "供应商编码",
        "供应商代码",
        "供应商编号",
        "vendor_code",
        "supplier_code",
    ),
    "amount": (
        "金额",
        "交易金额",
        "付款金额",
        "发生额",
        "amount",
        "payment_amount",
        "transaction_amount",
    ),
    "payment_id": (
        "付款单号",
        "付款编号",
        "付款流水号",
        "单据号",
        "payment_id",
        "payment_no",
        "payment_number",
    ),
    "invoice_no": (
        "发票号",
        "发票号码",
        "发票编号",
        "invoice_no",
        "invoice_number",
        "invoice",
    ),
    "description": (
        "摘要",
        "备注",
        "交易说明",
        "description",
        "memo",
        "remark",
    ),
    "department": (
        "部门",
        "申请部门",
        "所属部门",
        "department",
        "dept",
    ),
    "bank_account": (
        "银行账号",
        "银行账户",
        "收款账号",
        "账号",
        "bank_account",
        "account_no",
        "bank_account_no",
    ),
}

_PAREN_RE = re.compile(r"[（(][^）)]*[)）]")
_EMAIL_RE = re.compile(r"\s+")
_NEGATIVE_RE = re.compile(r"^\((.*)\)$")


def normalize_text(value: Any) -> str:
    """NFKC + collapse whitespace; used for headers and vendor matching."""

    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    return _EMAIL_RE.sub(" ", text).strip()


def _normalize_header(value: Any) -> str:
    text = normalize_text(value)
    text = _PAREN_RE.sub("", text)
    return text.replace(" ", "").lower()


_NORMALIZED_ALIASES: dict[str, str] = {
    _normalize_header(alias): field
    for field, aliases in FIELD_ALIASES.items()
    for alias in aliases
}


def _match_field(value: Any) -> str | None:
    return _NORMALIZED_ALIASES.get(_normalize_header(value))


def _suffix(filename: str, data: bytes) -> str:
    lowered = (filename or "").lower()
    for suffix in SUPPORTED_SUFFIXES:
        if lowered.endswith(suffix):
            return suffix
    if data.startswith(b"PK\x03\x04"):
        return ".xlsx"
    return ".csv"


def _decode_csv(data: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030", "utf-16", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValidationError("无法识别的 CSV 编码，请使用 UTF-8 或 GBK 保存文件")


def _read_rows_csv(data: bytes) -> list[list[Any]]:
    text = _decode_csv(data)
    sample = text[:4096]
    delimiter = ","
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
    except csv.Error:
        delimiter = "\t" if "\t" in sample and sample.count(",") < sample.count("\t") else ","
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    rows: list[list[Any]] = []
    for row in reader:
        if len(rows) >= MAX_ROWS:
            raise ValidationError(f"文件行数超过上限 {MAX_ROWS}")
        if len(row) > MAX_COLUMNS:
            raise ValidationError(f"文件列数超过上限 {MAX_COLUMNS}")
        rows.append(list(row))
    return rows


def _read_rows_excel(data: bytes) -> list[list[Any]]:
    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - openpyxl raises many parser errors
        raise ValidationError(f"无法解析 Excel 文件: {exc}") from exc
    try:
        sheet = workbook.active
        if sheet is None:
            raise ValidationError("Excel 文件中没有可用的工作表")
        if (sheet.max_row or 0) > MAX_ROWS:
            raise ValidationError(f"文件行数超过上限 {MAX_ROWS}")
        if (sheet.max_column or 0) > MAX_COLUMNS:
            raise ValidationError(f"文件列数超过上限 {MAX_COLUMNS}")
        rows: list[list[Any]] = []
        for row in sheet.iter_rows(values_only=True):
            if len(rows) >= MAX_ROWS:
                raise ValidationError(f"文件行数超过上限 {MAX_ROWS}")
            if len(row) > MAX_COLUMNS:
                raise ValidationError(f"文件列数超过上限 {MAX_COLUMNS}")
            rows.append(list(row))
        return rows
    finally:
        workbook.close()


def _read_rows(data: bytes, filename: str) -> list[list[Any]]:
    if not data:
        raise ValidationError("上传文件为空")
    if len(data) > MAX_PARSE_BYTES:
        raise ValidationError("文件超过解析大小上限")
    suffix = _suffix(filename, data)
    if suffix in SUPPORTED_MIME_SUFFIX:
        return _read_rows_excel(data)
    if suffix == ".csv":
        return _read_rows_csv(data)
    raise ValidationError(f"不支持的文件类型: {filename or '未知'}")


def _is_blank_row(row: list[Any]) -> bool:
    return all(normalize_text(cell) == "" for cell in row)


def _is_total_row(row: list[Any]) -> bool:
    head = " ".join(normalize_text(cell) for cell in row[:4])
    return bool(re.search(r"(合计|总计|小计|累计)", head))


def _cell(row: list[Any], index: int | None) -> Any:
    if index is None or index >= len(row):
        return None
    return row[index]


def _row_value(row: list[Any], mapping: dict[str, int], field: str) -> Any:
    return _cell(row, mapping.get(field))


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, Decimal):
        return format(value, "f")
    text = normalize_text(value)
    return text or None


def _identifier(value: Any) -> str | None:
    text = _text(value)
    if text is None:
        return None
    # Excel turns "000123" into 123; keep integral floats tidy.
    if re.fullmatch(r"\d+\.0+", text):
        return text.split(".", 1)[0]
    return text


_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%Y年%m月%d日",
    "%Y%m%d",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
)


def _date_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if 1 <= number <= 2958465:  # Excel serial date range
            try:
                return from_excel(number).date().isoformat()
            except (ValueError, OverflowError):
                return None
    if isinstance(value, Decimal):
        return _date_text(float(value))
    text = normalize_text(value)
    if not text:
        return None
    text = text.replace("年", "-").replace("月", "-").replace("日", "")
    text = text.split("T", 1)[0]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _amount_value(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    if isinstance(value, Decimal):
        return round(float(value), 2)
    text = normalize_text(value)
    if not text:
        return None
    negative = False
    parenthesized = _NEGATIVE_RE.match(text)
    if parenthesized:
        negative = True
        text = parenthesized.group(1)
    for token in ("人民币", "RMB", "CNY", "¥", "￥", "元", ",", "，", " "):
        text = text.replace(token, "")
    if text.startswith("-"):
        negative = not negative
        text = text[1:]
    if text.startswith("+"):
        text = text[1:]
    if not text:
        return None
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None
    if not amount.is_finite():
        return None
    value_float = round(float(amount), 2)
    return -value_float if negative else value_float


def _locate_header(rows: list[list[Any]]) -> tuple[int, dict[str, int], dict[str, str]]:
    best_index = -1
    best_mapping: dict[str, int] = {}
    best_headers: dict[str, str] = {}
    for index, row in enumerate(rows[:10]):
        mapping: dict[str, int] = {}
        headers: dict[str, str] = {}
        for column, cell in enumerate(row):
            field = _match_field(cell)
            if field and field not in mapping:
                mapping[field] = column
                headers[field] = normalize_text(cell)
        if len(mapping) > len(best_mapping):
            best_index, best_mapping, best_headers = index, mapping, headers
    if best_index < 0 or len(best_mapping) < 2:
        raise ValidationError(
            "无法识别交易表头，请确认前 10 行包含交易日期、供应商、金额等列"
        )
    if not {"date", "vendor", "amount"}.intersection(best_mapping):
        raise ValidationError("表头缺少交易日期、供应商或金额列")
    return best_index, best_mapping, best_headers


def parse_transactions(data: bytes, filename: str = "") -> dict[str, Any]:
    """Parse spreadsheet bytes into normalized transactions.

    Returns ``transactions``, ``field_mapping``, ``header_row`` and
    ``quality_issues``. Row numbers are 1-based spreadsheet rows.
    """

    rows = _read_rows(data, filename)
    header_index, mapping, headers = _locate_header(rows)
    header_row = header_index + 1

    quality_issues: list[dict[str, Any]] = []
    for field in REQUIRED_FIELDS:
        if field not in mapping:
            quality_issues.append(
                {
                    "row_number": header_row,
                    "field": field,
                    "issue": "missing_column",
                    "message": f"缺少必需列: {field}",
                    "value": None,
                }
            )

    transactions: list[dict[str, Any]] = []
    for offset, row in enumerate(rows[header_index + 1 :]):
        row_number = header_row + 1 + offset
        if _is_blank_row(row) or _is_total_row(row):
            continue

        date_raw = _cell(row, mapping.get("date"))
        date_value = _date_text(date_raw)
        if date_raw is not None and date_value is None:
            quality_issues.append(
                {
                    "row_number": row_number,
                    "field": "date",
                    "issue": "unparsable_date",
                    "message": "无法解析交易日期",
                    "value": _text(date_raw),
                }
            )

        amount_raw = _row_value(row, mapping, "amount")
        amount_value = _amount_value(amount_raw)
        if amount_raw is not None and normalize_text(amount_raw) != "" and amount_value is None:
            quality_issues.append(
                {
                    "row_number": row_number,
                    "field": "amount",
                    "issue": "unparsable_amount",
                    "message": "无法解析交易金额",
                    "value": _text(amount_raw),
                }
            )

        transaction = {
            "row_number": row_number,
            "transaction_id": f"T{row_number:04d}",
            "date": date_value,
            "vendor": _text(_row_value(row, mapping, "vendor")),
            "vendor_code": _identifier(_row_value(row, mapping, "vendor_code")),
            "amount": amount_value,
            "payment_id": _identifier(_row_value(row, mapping, "payment_id")),
            "invoice_no": _identifier(_row_value(row, mapping, "invoice_no")),
            "description": _text(_row_value(row, mapping, "description")),
            "department": _text(_row_value(row, mapping, "department")),
            "bank_account": _identifier(_row_value(row, mapping, "bank_account")),
        }

        if transaction["date"] is None and transaction["vendor"] is None:
            if transaction["amount"] is None and transaction["payment_id"] is None:
                continue

        for field in REQUIRED_FIELDS:
            value = transaction[field]
            if value is None or value == "":
                quality_issues.append(
                    {
                        "row_number": row_number,
                        "field": field,
                        "issue": "missing_field",
                        "message": f"缺少必需字段: {field}",
                        "value": None,
                    }
                )

        transactions.append(transaction)

    return {
        "filename": filename,
        "header_row": header_row,
        "field_mapping": headers,
        "transactions": transactions,
        "quality_issues": quality_issues,
    }
