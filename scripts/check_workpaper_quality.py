#!/usr/bin/env python3
"""Deployment smoke check for the accountant workpaper artifacts.

This is intentionally conservative: it blocks broken or structurally unsafe
workbooks, but leaves genuine accounting judgement as visible review notes.
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.etree import ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from accountant_copilot.tb_bridge_workflow import (  # noqa: E402
    TB_BRIDGE_JSON,
    TB_BRIDGE_XLSX,
    validate_relationship_register,
    validate_tb_bridge_workpaper,
)


EXPECTED_SHEETS = {"TB Bridge", "Movement Notes", "Evidence Index"}
TOLERANCE = Decimal("0.01")


def _load_json(path: Path) -> tuple[dict, str | None]:
    if not path.exists():
        return {}, f"Missing required artifact: {path}"
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return {}, f"Invalid JSON in {path}: {exc}"
    if not isinstance(payload, dict):
        return {}, f"Expected JSON object in {path}"
    return payload, None


def _decimal(value: object) -> Decimal | None:
    raw = str(value or "").replace("$", "").replace(",", "").strip()
    if not raw:
        return Decimal("0")
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _severity_counts(findings: list[dict]) -> dict[str, int]:
    counts = {"high": 0, "medium": 0, "low": 0}
    for finding in findings:
        severity = str(finding.get("severity") or "medium").lower()
        counts[severity] = counts.get(severity, 0) + 1
    return counts


def _blocking_findings(findings: list[dict]) -> list[dict]:
    return [finding for finding in findings if isinstance(finding, dict) and finding.get("severity") == "high"]


def _matrix_math_findings(tb_payload: dict) -> list[dict]:
    findings: list[dict] = []
    columns = {
        str(column.get("column_key") or ""): str(column.get("label") or column.get("column_key") or "")
        for column in tb_payload.get("movement_columns") or []
        if isinstance(column, dict) and column.get("column_key")
    }
    totals = {key: Decimal("0") for key in columns}
    for row in tb_payload.get("matrix_rows") or []:
        if not isinstance(row, dict):
            continue
        account_name = str(row.get("account_name") or "<missing account>")
        opening = _decimal(row.get("opening_balance"))
        closing = _decimal(row.get("closing_balance"))
        if opening is None or closing is None:
            findings.append(
                {
                    "category": "invalid_row_balance",
                    "severity": "high",
                    "message": f"{account_name} has a non-numeric opening or closing balance.",
                }
            )
            continue
        movement_total = Decimal("0")
        for movement in row.get("movements") or []:
            if not isinstance(movement, dict):
                continue
            key = str(movement.get("column_key") or "")
            amount = _decimal(movement.get("amount"))
            if amount is None:
                findings.append(
                    {
                        "category": "invalid_movement_amount",
                        "severity": "high",
                        "message": f"{account_name} has a non-numeric movement amount.",
                    }
                )
                continue
            movement_total += amount
            if key in totals:
                totals[key] += amount
        difference = opening + movement_total - closing
        if abs(difference) > TOLERANCE:
            findings.append(
                {
                    "category": "row_rollforward_mismatch",
                    "severity": "high",
                    "message": f"{account_name} does not reconcile: opening + movements - closing = {difference:.2f}.",
                }
            )
    for key, total in totals.items():
        if abs(total) > TOLERANCE:
            findings.append(
                {
                    "category": "movement_column_not_balanced",
                    "severity": "high",
                    "message": f"{columns[key]} movement column totals {total:.2f}, not 0.00.",
                }
            )
    return findings


def _workbook_findings(xlsx_path: Path) -> list[dict]:
    findings: list[dict] = []
    if not xlsx_path.exists():
        return [{"category": "missing_workbook", "severity": "high", "message": f"Workbook not found: {xlsx_path}"}]
    try:
        with zipfile.ZipFile(xlsx_path) as workbook:
            bad_member = workbook.testzip()
            if bad_member:
                findings.append(
                    {
                        "category": "corrupt_workbook_zip_member",
                        "severity": "high",
                        "message": f"Workbook zip member failed integrity check: {bad_member}",
                    }
                )
            ns = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            root = ET.fromstring(workbook.read("xl/workbook.xml"))
            sheet_names = {sheet.attrib.get("name", "") for sheet in root.find("main:sheets", ns) or []}
            missing = sorted(EXPECTED_SHEETS - sheet_names)
            if missing:
                findings.append(
                    {
                        "category": "missing_workbook_sheets",
                        "severity": "high",
                        "message": f"Workbook is missing expected sheet(s): {', '.join(missing)}.",
                    }
                )
            evidence_rels = "xl/worksheets/_rels/sheet3.xml.rels"
            if evidence_rels not in workbook.namelist():
                findings.append(
                    {
                        "category": "missing_evidence_hyperlinks",
                        "severity": "medium",
                        "message": "Evidence Index hyperlink relationship file is missing.",
                    }
                )
            else:
                rel_root = ET.fromstring(workbook.read(evidence_rels))
                rel_count = len(list(rel_root))
                if rel_count == 0:
                    findings.append(
                        {
                            "category": "empty_evidence_hyperlinks",
                            "severity": "medium",
                            "message": "Evidence Index has no hyperlink relationships.",
                        }
                    )
    except (KeyError, OSError, zipfile.BadZipFile, ET.ParseError) as exc:
        findings.append({"category": "invalid_workbook", "severity": "high", "message": f"Workbook cannot be inspected: {exc}"})
    return findings


def check_workpaper_quality(artifact_dir: Path, output_dir: Path) -> dict:
    source_index, source_error = _load_json(artifact_dir / "source_document_index.json")
    relationship_register, relationship_error = _load_json(artifact_dir / "relationship_reasoning_register.json")
    prior_coa, prior_error = _load_json(artifact_dir / "prior_statement_coa_import.json")
    tb_payload, tb_error = _load_json(output_dir / TB_BRIDGE_JSON)
    turing_payload, turing_error = _load_json(output_dir / "turing_senior_review.json")

    findings: list[dict] = []
    for error in [source_error, relationship_error, prior_error, tb_error, turing_error]:
        if error:
            findings.append({"category": "missing_or_invalid_artifact", "severity": "high", "message": error})
    if source_index and relationship_register:
        findings.extend(validate_relationship_register(relationship_register, source_index))
    if tb_payload and relationship_register:
        findings.extend(validate_tb_bridge_workpaper(tb_payload, relationship_register, prior_coa))
        findings.extend(_matrix_math_findings(tb_payload))
    findings.extend(_workbook_findings(output_dir / TB_BRIDGE_XLSX))

    if turing_payload:
        if turing_payload.get("status") != "ready":
            findings.append(
                {
                    "category": "turing_review_not_ready",
                    "severity": "high",
                    "message": f"Turing review status is {turing_payload.get('status') or 'missing'}, not ready.",
                }
            )
        correction_briefs = turing_payload.get("correction_briefs") if isinstance(turing_payload.get("correction_briefs"), list) else []
        if correction_briefs:
            findings.append(
                {
                    "category": "open_turing_correction_briefs",
                    "severity": "high",
                    "message": f"Turing review still has {len(correction_briefs)} correction brief(s).",
                }
            )

    summary_path = output_dir / "prepared_workpaper_summary.md"
    workbook_path = output_dir / TB_BRIDGE_XLSX
    if summary_path.exists() and workbook_path.exists() and summary_path.stat().st_mtime < workbook_path.stat().st_mtime:
        findings.append(
            {
                "category": "stale_prepared_summary",
                "severity": "medium",
                "message": "prepared_workpaper_summary.md is older than the workbook.",
            }
        )

    blocking = _blocking_findings(findings)
    tb_summary = tb_payload.get("summary") if isinstance(tb_payload.get("summary"), dict) else {}
    result = {
        "status": "pass" if not blocking else "fail",
        "artifact_dir": str(artifact_dir),
        "output_dir": str(output_dir),
        "workbook": str(output_dir / TB_BRIDGE_XLSX),
        "summary": {
            "documents": len(source_index.get("documents") or []) if source_index else 0,
            "relationships": len(relationship_register.get("relationships") or []) if relationship_register else 0,
            "matrix_rows": tb_summary.get("matrix_rows", 0),
            "movement_columns": tb_summary.get("movement_columns", 0),
            "movement_notes": tb_summary.get("movement_notes", 0),
            "turing_status": turing_payload.get("status") if turing_payload else "",
        },
        "severity_counts": _severity_counts(findings),
        "blocking_findings": blocking,
        "findings": findings,
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Check accountant workpaper artifacts for deployment-blocking quality issues.")
    parser.add_argument("--artifact-dir", default="outputs/raw_inputs_pdf_extraction")
    parser.add_argument("--output-dir", default="outputs/step4_tb_bridge_workpaper")
    parser.add_argument("--json-output", default=None)
    args = parser.parse_args()

    result = check_workpaper_quality(Path(args.artifact_dir), Path(args.output_dir))
    output_path = Path(args.json_output) if args.json_output else Path(args.output_dir) / "workpaper_quality_check.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, sort_keys=True))

    print(f"Workpaper quality check: {result['status']}")
    print(f"Artifacts: {result['summary']}")
    print(f"Severity counts: {result['severity_counts']}")
    if result["blocking_findings"]:
        print("Blocking findings:")
        for finding in result["blocking_findings"][:12]:
            print(f"- {finding.get('category')}: {finding.get('message')}")
    else:
        non_blocking = [finding for finding in result["findings"] if finding not in result["blocking_findings"]]
        if non_blocking:
            print("Non-blocking review notes:")
            for finding in non_blocking[:8]:
                print(f"- {finding.get('severity', 'medium')} / {finding.get('category')}: {finding.get('message')}")
    print(f"Wrote {output_path}")
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
