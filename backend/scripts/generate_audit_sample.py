"""Generate the deterministic audit demo workbook and anomaly labels.

Run from the repository backend directory:

    uv run python scripts/generate_audit_sample.py

The workbook contains a title row, a Chinese/English header row, normal
transactions and injected anomalies. ``seed/audit_labels.json`` records the
injected duplicate/large/rapid anomalies so tests can measure precision and
recall.
"""

from __future__ import annotations

import json
import random
import sys
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SEED = 20240101
HEADER_ROW = 2
LABELED_RULES = ("duplicate_payment", "large_transaction", "rapid_repeat")

HEADERS = [
    "交易日期",
    "付款单号",
    "供应商编码",
    "供应商",
    "发票号",
    "金额",
    "摘要",
    "部门",
    "银行账号",
]

VENDORS: list[tuple[str, str, str]] = [
    ("深圳市宏远电子科技有限公司", "V001", "6222020200001001"),
    ("上海嘉合贸易有限公司", "V002", "6222020200001002"),
    ("北京中科智联系统集成有限公司", "V003", "6222020200001003"),
    ("广州天成物流有限公司", "V004", "6222020200001004"),
    ("杭州迅捷物流有限公司", "V005", "6222020200001005"),
    ("成都恒信建材有限公司", "V006", "6222020200001006"),
    ("南京博远信息技术有限公司", "V007", "6222020200001007"),
    ("武汉长江机电设备有限公司", "V008", "6222020200001008"),
    ("苏州精诚模具有限公司", "V009", "6222020200001009"),
    ("天津海通化工原料有限公司", "V010", "6222020200001010"),
    ("西安华瑞电力工程有限公司", "V011", "6222020200001011"),
    ("青岛蓝海包装材料有限公司", "V012", "6222020200001012"),
]

DESCRIPTIONS = [
    "采购原材料货款",
    "设备维护费用",
    "物流运输费",
    "技术咨询服务费",
    "工程进度款",
    "办公用品采购",
    "生产辅料采购",
    "仓储租赁费",
]
DEPARTMENTS = ["采购部", "财务部", "工程部", "运营部", "行政部"]


class DatasetBuilder:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.labels: list[dict[str, Any]] = []
        self._sequence = 0

    def _next_id(self, prefix: str) -> str:
        self._sequence += 1
        return f"{prefix}2024{self._sequence:06d}"

    def row_number(self) -> int:
        return len(self.records) + HEADER_ROW

    def add(
        self,
        *,
        when: date,
        vendor: tuple[str, str, str],
        amount: Any,
        description: str = "",
        department: str = "",
        payment_id: str | None = None,
        invoice_no: str | None = None,
        label_rule: str | None = None,
    ) -> int:
        record = {
            "交易日期": when,
            "付款单号": payment_id if payment_id is not None else self._next_id("PAY"),
            "供应商编码": vendor[1],
            "供应商": vendor[0],
            "发票号": invoice_no if invoice_no is not None else self._next_id("FP"),
            "金额": amount,
            "摘要": description or DESCRIPTIONS[self._sequence % len(DESCRIPTIONS)],
            "部门": department or DEPARTMENTS[self._sequence % len(DEPARTMENTS)],
            "银行账号": vendor[2],
        }
        self.records.append(record)
        row_number = self.row_number()
        if label_rule:
            self.labels.append(
                {
                    "rule": label_rule,
                    "row_number": row_number,
                    "payment_id": record["付款单号"],
                }
            )
        return row_number

    def add_duplicate_of(self, base_index: int, when: date) -> int:
        base = deepcopy(self.records[base_index])
        base["交易日期"] = when
        base["付款单号"] = self._next_id("PAY")
        return self.add(
            when=when,
            vendor=(base["供应商"], base["供应商编码"], base["银行账号"]),
            amount=base["金额"],
            description=base["摘要"],
            department=base["部门"],
            payment_id=base["付款单号"],
            invoice_no=base["发票号"],
            label_rule="duplicate_payment",
        )


def build_dataset() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rng = random.Random(SEED)
    builder = DatasetBuilder()
    normal_vendors = [vendor for vendor in VENDORS if vendor[1] != "V005"]
    start = date(2024, 1, 3)

    for index in range(66):
        vendor = normal_vendors[index % len(normal_vendors)]
        when = start + timedelta(days=index * 3 + rng.randrange(2))
        amount = round(rng.uniform(1_500, 62_000), 2)
        builder.add(
            when=when,
            vendor=vendor,
            amount=amount,
            description=DESCRIPTIONS[(index + rng.randrange(len(DESCRIPTIONS))) % len(DESCRIPTIONS)],
            department=DEPARTMENTS[(index + rng.randrange(len(DEPARTMENTS))) % len(DEPARTMENTS)],
        )

    # Boundary row just below the default large threshold.
    builder.add(
        when=date(2024, 2, 2),
        vendor=normal_vendors[2],
        amount=99_999.99,
        description="设备采购首付款",
    )

    # Large transactions, all at or above the 100000 threshold.
    large_specs = [
        (date(2024, 2, 14), VENDORS[2], 100_000.00),
        (date(2024, 3, 11), VENDORS[6], 188_888.88),
        (date(2024, 5, 6), VENDORS[1], 256_000.00),
        (date(2024, 6, 18), VENDORS[8], 399_999.99),
        (date(2024, 7, 2), VENDORS[10], 1_280_000.00),
    ]
    for when, vendor, amount in large_specs:
        builder.add(
            when=when,
            vendor=vendor,
            amount=amount,
            description="大额设备采购款",
            label_rule="large_transaction",
        )

    # Duplicate payments: same vendor + amount + invoice, new payment id.
    for base_index, delay_days in ((4, 2), (17, 20), (29, 1), (45, 35)):
        base_date = builder.records[base_index]["交易日期"]
        builder.add_duplicate_of(base_index, base_date + timedelta(days=delay_days))

    # Rapid repeat cluster: four payments to the same vendor within six days.
    rapid_vendor = VENDORS[4]
    rapid_dates = [date(2024, 8, 5), date(2024, 8, 6), date(2024, 8, 8), date(2024, 8, 10)]
    rapid_amounts = [15_800.00, 17_200.00, 21_900.00, 12_600.00]
    for index, (when, amount) in enumerate(zip(rapid_dates, rapid_amounts, strict=True)):
        builder.add(
            when=when,
            vendor=rapid_vendor,
            amount=amount,
            description="临时运输费用",
            label_rule="rapid_repeat" if index >= 2 else None,
        )

    # Data-quality rows: negative amount, missing payment id, unparsable amount.
    builder.add(
        when=date(2024, 9, 3),
        vendor=normal_vendors[5],
        amount=-8_800.00,
        description="退款冲销",
    )
    builder.add(
        when=date(2024, 9, 6),
        vendor=normal_vendors[7],
        amount=3_300.00,
        description="办公耗材采购",
        payment_id="",
    )
    builder.add(
        when=date(2024, 9, 9),
        vendor=normal_vendors[9],
        amount="金额待定",
        description="合同金额待补录",
    )

    return builder.records, builder.labels


def write_workbook(records: list[dict[str, Any]], path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "交易明细"

    sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(HEADERS))
    title_cell = sheet.cell(row=1, column=1, value="华信集团 2024 年度财务交易明细")
    title_cell.font = Font(size=14, bold=True)
    title_cell.alignment = Alignment(horizontal="center")

    header_fill = PatternFill("solid", fgColor="D9E1F2")
    for column, header in enumerate(HEADERS, start=1):
        cell = sheet.cell(row=HEADER_ROW, column=column, value=header)
        cell.font = Font(bold=True)
        cell.fill = header_fill

    for offset, record in enumerate(records):
        row = HEADER_ROW + 1 + offset
        for column, header in enumerate(HEADERS, start=1):
            sheet.cell(row=row, column=column, value=record[header])

    last_row = HEADER_ROW + len(records)
    total_row = last_row + 1
    sheet.cell(row=total_row, column=1, value="合计")
    sheet.cell(row=total_row, column=6, value=f"=SUM(F3:F{last_row})")

    widths = [14, 18, 12, 30, 18, 16, 24, 10, 22]
    for column, width in enumerate(widths, start=1):
        sheet.column_dimensions[chr(64 + column)].width = width

    workbook.properties.creator = "BaseAgent Audit Sample"
    workbook.properties.lastModifiedBy = "BaseAgent Audit Sample"
    workbook.properties.created = datetime(2024, 1, 1, 0, 0, 0)
    workbook.properties.modified = datetime(2024, 1, 1, 0, 0, 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)


def verify(path: Path, labels: list[dict[str, Any]]) -> dict[str, Any]:
    from app.domain_plugins.audit.analysis import analyze_transactions
    from app.domain_plugins.audit.excel import parse_transactions

    parsed = parse_transactions(path.read_bytes(), path.name)
    result = analyze_transactions(parsed["transactions"])
    detected = {
        (anomaly["rule"], anomaly["row_number"])
        for anomaly in result["anomalies"]
        if anomaly["rule"] in LABELED_RULES
    }
    expected = {(label["rule"], label["row_number"]) for label in labels}
    if detected != expected:
        extra = sorted(detected - expected)
        missing = sorted(expected - detected)
        raise SystemExit(f"label mismatch: extra={extra} missing={missing}")

    true_positives = len(detected & expected)
    precision = true_positives / len(detected) if detected else 1.0
    recall = true_positives / len(expected) if expected else 1.0
    quality_rows = sorted(
        {
            issue["row_number"]
            for issue in parsed["quality_issues"]
            if issue["issue"] in {"unparsable_amount", "missing_field", "missing_column"}
        }
    )
    return {
        "transactions": len(parsed["transactions"]),
        "anomalies": len(result["anomalies"]),
        "labeled": len(expected),
        "precision": precision,
        "recall": recall,
        "quality_issue_rows": quality_rows,
        "summary": result["summary"],
    }


def main() -> None:
    records, labels = build_dataset()
    seed_dir = ROOT / "seed"
    workbook_path = seed_dir / "audit_sample.xlsx"
    labels_path = seed_dir / "audit_labels.json"
    write_workbook(records, workbook_path)

    labels_payload = {
        "file": workbook_path.name,
        "seed": SEED,
        "row_count": len(records),
        "labeled_rules": list(LABELED_RULES),
        "anomalies": sorted(
            labels, key=lambda item: (item["row_number"], item["rule"])
        ),
    }
    labels_path.write_text(
        json.dumps(labels_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    metrics = verify(workbook_path, labels)
    print(
        "generated {file} rows={rows} anomalies={anomalies} labeled={labeled} "
        "precision={precision:.3f} recall={recall:.3f}".format(
            file=workbook_path,
            rows=metrics["transactions"],
            anomalies=metrics["anomalies"],
            labeled=metrics["labeled"],
            precision=metrics["precision"],
            recall=metrics["recall"],
        )
    )
    print(json.dumps(metrics["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
