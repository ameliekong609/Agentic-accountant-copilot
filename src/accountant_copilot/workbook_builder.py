"""Excel workbook builder generation and hyperlink repair utilities."""
from __future__ import annotations

import json
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from accountant_copilot.contract_utils import TB_BRIDGE_JSON, TB_BRIDGE_XLSX

def write_tb_bridge_workbook_builder(output_dir: Path, node_modules_dir: str | None = None) -> Path:
    builder = output_dir / "build_tb_bridge_workpaper.mjs"
    node_modules = node_modules_dir or "/Users/ameliekong/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules"
    builder.write_text(
        f"""import fs from 'node:fs/promises';
import path from 'node:path';
import {{ SpreadsheetFile, Workbook }} from '@oai/artifact-tool';

const outputDir = {json.dumps(str(output_dir.resolve()))};
const payload = JSON.parse(await fs.readFile(path.join(outputDir, {json.dumps(TB_BRIDGE_JSON)}), 'utf8'));
const workbook = Workbook.create();

function text(value) {{ return value === undefined || value === null ? '' : String(value); }}
function numberValue(value) {{
  const n = Number(text(value).replace(/[$,]/g, ''));
  return Number.isFinite(n) ? n : null;
}}
function money(value) {{
  const n = numberValue(value);
  return n === null ? '' : n;
}}
function colName(index) {{
  let name = '';
  let n = index + 1;
  while (n > 0) {{
    const r = (n - 1) % 26;
    name = String.fromCharCode(65 + r) + name;
    n = Math.floor((n - 1) / 26);
  }}
  return name;
}}
function writeTable(sheet, startRow, startCol, rows) {{
  if (!rows.length) return;
  sheet.getRangeByIndexes(startRow, startCol, rows.length, rows[0].length).values = rows;
}}
function styleHeader(range) {{
  range.format.fill.color = '#163f4d';
  range.format.font.color = '#ffffff';
  range.format.font.bold = true;
  range.format.wrapText = true;
}}
function styleSubHeader(range) {{
  range.format.fill.color = '#e7f1ef';
  range.format.font.color = '#12343b';
  range.format.font.bold = true;
}}
function styleCurrency(range) {{
  range.setNumberFormat('$#,##0.00;[Red]($#,##0.00);-');
}}
function supportFill(support) {{
  if (support === 'direct_evidence') return '#e8f6ef';
  if (support === 'evidence_derived') return '#eef4ff';
  if (support === 'judgement') return '#fff7df';
  if (support === 'unsupported') return '#fdecec';
  return '';
}}

const matrix = workbook.worksheets.add('TB Bridge');
matrix.showGridLines = false;
const columns = Array.isArray(payload.movement_columns) ? payload.movement_columns : [];
const rows = Array.isArray(payload.matrix_rows) ? payload.matrix_rows : [];
const headers = ['Section', 'Group', 'Account', 'Opening balance', 'PY comparative', ...columns.map(c => text(c.label || c.column_key)), 'Closing', 'Difference', 'Status', 'Note ID'];
const table = [headers];
for (const row of rows) {{
  const byColumn = new Map();
  for (const movement of Array.isArray(row.movements) ? row.movements : []) {{
    const key = text(movement.column_key);
    const existing = Number(byColumn.get(key)?.amount || 0);
    const amount = Number(money(movement.amount) || 0);
    const support = text(movement.support_type);
    byColumn.set(key, {{ amount: existing + amount, support, explanation: text(movement.explanation) }});
  }}
  table.push([
    text(row.statement_section),
    text(row.statement_group),
    text(row.account_name),
    money(row.opening_balance),
    money(row.prior_year_comparative),
    ...columns.map(c => {{
      const cell = byColumn.get(text(c.column_key));
      return cell ? cell.amount : '';
    }}),
    money(row.closing_balance),
    money(row.difference),
    text(row.row_status),
    Array.isArray(row.note_ids) && row.note_ids.length ? row.note_ids.join(', ') : text(row.notes),
  ]);
}}
writeTable(matrix, 0, 0, table);
const totalRow = rows.length + 1;
matrix.getCell(totalRow, 2).values = [['Column total']];
for (let c = 0; c < columns.length; c++) {{
  const col = colName(5 + c);
  matrix.getCell(totalRow, 5 + c).formulas = [[`=SUM(${{col}}2:${{col}}${{rows.length + 1}})`]];
}}
styleHeader(matrix.getRangeByIndexes(0, 0, 1, headers.length));
matrix.freezePanes.freezeRows(1);
matrix.freezePanes.freezeColumns(3);
styleCurrency(matrix.getRangeByIndexes(1, 3, Math.max(rows.length + 1, 1), Math.max(columns.length + 4, 1)));
matrix.getRangeByIndexes(0, 0, table.length + 1, headers.length).format.borders = {{ preset: 'all', style: 'thin', color: '#d8e2e7' }};
matrix.getRangeByIndexes(0, 0, table.length + 1, headers.length).format.wrapText = true;
for (let r = 0; r < rows.length; r++) {{
  const row = rows[r];
  const byColumn = new Map();
  for (const movement of Array.isArray(row.movements) ? row.movements : []) {{
    byColumn.set(text(movement.column_key), text(movement.support_type));
  }}
  const section = text(row.statement_section);
  const sectionCell = matrix.getCell(r + 1, 0);
  if (section === 'Balance sheet') sectionCell.format.fill.color = '#edf7f5';
  if (section === 'Profit and loss') sectionCell.format.fill.color = '#f1f5fb';
  if (section === 'Clearing / attention') sectionCell.format.fill.color = '#fff4e6';
  for (let c = 0; c < columns.length; c++) {{
    const fill = supportFill(byColumn.get(text(columns[c].column_key)));
    if (fill) matrix.getCell(r + 1, 5 + c).format.fill.color = fill;
  }}
  const status = text(row.row_status);
  if (status === 'needs_attention') matrix.getCell(r + 1, headers.length - 2).format.fill.color = '#fff4e6';
  if (status === 'ready') matrix.getCell(r + 1, headers.length - 2).format.fill.color = '#e8f6ef';
  if (status === 'excluded') matrix.getCell(r + 1, headers.length - 2).format.fill.color = '#f1f5f9';
}}
matrix.getRangeByIndexes(totalRow, 0, 1, headers.length).format.fill.color = '#f6f8fa';
matrix.getRangeByIndexes(totalRow, 0, 1, headers.length).format.font.bold = true;
matrix.getRangeByIndexes(0, 0, table.length + 1, headers.length).format.autofitColumns();
matrix.getRangeByIndexes(0, 0, table.length + 1, Math.min(headers.length, 12)).format.wrapText = true;
if (headers.length > 12) {{
  matrix.getRangeByIndexes(0, 12, table.length + 1, headers.length - 12).format.wrapText = false;
}}
matrix.getRangeByIndexes(0, 0, table.length + 1, Math.min(headers.length, 3)).format.font.bold = true;
matrix.getRange('A:A').format.columnWidth = 18;
matrix.getRange('B:B').format.columnWidth = 28;
matrix.getRange('C:C').format.columnWidth = 42;
matrix.getRangeByIndexes(0, headers.length - 1, table.length + 1, 1).format.columnWidth = 16;
matrix.getRangeByIndexes(0, headers.length - 1, table.length + 1, 1).format.wrapText = true;

const notes = workbook.worksheets.add('Movement Notes');
notes.showGridLines = false;
const noteRows = [['TB Row', 'Section', 'Group', 'Account', 'Status', 'Note ID', 'TB Column', 'Opening', 'Closing', 'Main amount', 'Other amounts', 'Explanation', 'Calculation', 'Evidence', 'Relationships']];
for (const note of Array.isArray(payload.movement_notes) ? payload.movement_notes : []) {{
  noteRows.push([
    text(note.tb_row),
    text(note.statement_section),
    text(note.statement_group),
    text(note.account_name),
    text(note.status),
    text(note.note_id),
    text(note.tb_column),
    money(note.opening_balance),
    money(note.closing_balance),
    money(note.main_amount),
    text(note.other_amounts),
    text(note.explanation),
    text(note.calculation),
    text(note.evidence_summary),
    Array.isArray(note.relationship_ids) ? note.relationship_ids.join(', ') : '',
  ]);
}}
writeTable(notes, 0, 0, noteRows);
styleHeader(notes.getRange('A1:O1'));
notes.freezePanes.freezeRows(1);
notes.freezePanes.freezeColumns(4);
notes.getRangeByIndexes(0, 0, noteRows.length, 15).format.borders = {{ preset: 'all', style: 'thin', color: '#d8e2e7' }};
styleCurrency(notes.getRangeByIndexes(1, 7, Math.max(noteRows.length - 1, 1), 3));
notes.getRange('A:O').format.autofitColumns();
notes.getRange('A:A').format.columnWidth = 10;
notes.getRange('B:B').format.columnWidth = 18;
notes.getRange('C:C').format.columnWidth = 24;
notes.getRange('D:D').format.columnWidth = 42;
notes.getRange('G:G').format.columnWidth = 30;
notes.getRange('K:K').format.columnWidth = 34;
notes.getRange('L:L').format.columnWidth = 78;
notes.getRange('M:M').format.columnWidth = 48;
notes.getRange('N:N').format.columnWidth = 54;
notes.getRange('A:O').format.wrapText = true;
const noteCount = Array.isArray(payload.movement_notes) ? payload.movement_notes.length : 0;
for (let r = 0; r < noteCount; r++) {{
  const note = payload.movement_notes[r];
  const status = text(note.status);
  if (status === 'needs_attention') notes.getCell(r + 1, 4).format.fill.color = '#fff4e6';
  if (status === 'ready') notes.getCell(r + 1, 4).format.fill.color = '#e8f6ef';
  if (status === 'excluded' || status === 'not_posted') notes.getCell(r + 1, 4).format.fill.color = '#f1f5f9';
}}

const evidence = workbook.worksheets.add('Evidence Index');
evidence.showGridLines = false;
const documentRows = [['Display name', 'Type', 'Entity relevance', 'Period / date', 'Summary', 'PDF']];
const linkFormulas = [];
function excelQuote(value) {{ return text(value).replace(/"/g, '""'); }}
function fileUrl(value) {{
  const raw = text(value);
  if (!raw) return '';
  const absolute = path.isAbsolute(raw) ? raw : path.resolve(raw);
  return 'file://' + absolute.split(path.sep).map(encodeURIComponent).join('/');
}}
for (const document of Array.isArray(payload.source_documents) ? payload.source_documents : []) {{
  documentRows.push([
    text(document.display_name),
    text(document.document_type),
    text(document.entity_relevance),
    [text(document.period_start), text(document.period_end)].filter(Boolean).join(' to ') || text(document.statement_date),
    text(document.summary),
    '',
  ]);
  const url = fileUrl(document.file_path);
  linkFormulas.push([url ? `=HYPERLINK("${{excelQuote(url)}}","Click here")` : '']);
}}
writeTable(evidence, 0, 0, documentRows);
if (linkFormulas.length) {{
  evidence.getRangeByIndexes(1, 5, linkFormulas.length, 1).formulas = linkFormulas;
}}
styleHeader(evidence.getRange('A1:F1'));
evidence.getRangeByIndexes(0, 0, documentRows.length, 6).format.borders = {{ preset: 'all', style: 'thin', color: '#d8e2e7' }};
evidence.getRange('A:F').format.autofitColumns();
evidence.getRange('A:A').format.columnWidth = 42;
evidence.getRange('E:E').format.columnWidth = 60;
evidence.getRange('A:F').format.wrapText = true;

const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(path.join(outputDir, {json.dumps(TB_BRIDGE_XLSX)}));
console.log(path.join(outputDir, {json.dumps(TB_BRIDGE_XLSX)}));
""",
        encoding="utf-8",
    )
    node_modules_path = output_dir / "node_modules"
    source_modules = Path(node_modules)
    if source_modules.exists() and not node_modules_path.exists():
        try:
            node_modules_path.symlink_to(source_modules, target_is_directory=True)
        except FileExistsError:
            pass
    return builder

def repair_tb_bridge_workbook_hyperlinks(xlsx_path: Path) -> int:
    """Convert cached unsupported HYPERLINK formulas into real Excel hyperlinks.

    The artifact workbook engine can write HYPERLINK formulas but caches them as
    unsupported formula results. Excel will sometimes recalculate them, but the
    safer accountant-facing output is a normal hyperlink relationship with
    visible "Click here" text.
    """

    if not xlsx_path.exists():
        return 0

    main_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    pkg_rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    hyperlink_rel_type = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
    relationship_tag = f"{{{pkg_rel_ns}}}Relationship"
    ET.register_namespace("", main_ns)
    ET.register_namespace("r", rel_ns)

    formula_re = re.compile(r'^HYPERLINK\("(?P<url>(?:[^"]|"")*)","(?P<label>(?:[^"]|"")*)"\)$')

    with zipfile.ZipFile(xlsx_path, "r") as zin:
        workbook_xml = ET.fromstring(zin.read("xl/workbook.xml"))
        workbook_rels_xml = ET.fromstring(zin.read("xl/_rels/workbook.xml.rels"))
        rel_targets = {rel.attrib["Id"]: rel.attrib["Target"] for rel in workbook_rels_xml}

        evidence_target: str | None = None
        for sheet in workbook_xml.findall(f".//{{{main_ns}}}sheet"):
            if sheet.attrib.get("name") == "Evidence Index":
                rid = sheet.attrib.get(f"{{{rel_ns}}}id")
                target = rel_targets.get(rid or "")
                if target:
                    evidence_target = target.lstrip("/")
                    if not evidence_target.startswith("xl/"):
                        evidence_target = "xl/" + evidence_target
                break

        if not evidence_target or evidence_target not in zin.namelist():
            return 0

        sheet_xml = ET.fromstring(zin.read(evidence_target))
        repaired: list[tuple[str, str, str]] = []
        for cell in sheet_xml.findall(f".//{{{main_ns}}}c"):
            formula = cell.find(f"{{{main_ns}}}f")
            if formula is None or not formula.text:
                continue
            match = formula_re.match(formula.text)
            if not match:
                continue
            url = match.group("url").replace('""', '"')
            label = match.group("label").replace('""', '"') or "Click here"
            ref = cell.attrib.get("r")
            if not ref:
                continue

            for child in list(cell):
                cell.remove(child)
            cell.attrib["t"] = "inlineStr"
            inline = ET.SubElement(cell, f"{{{main_ns}}}is")
            text_node = ET.SubElement(inline, f"{{{main_ns}}}t")
            text_node.text = label
            repaired.append((ref, url, label))

        if not repaired:
            return 0

        existing_hyperlinks = sheet_xml.find(f"{{{main_ns}}}hyperlinks")
        if existing_hyperlinks is not None:
            sheet_xml.remove(existing_hyperlinks)
        hyperlinks = ET.Element(f"{{{main_ns}}}hyperlinks")

        rels_path = str(Path(evidence_target).parent / "_rels" / (Path(evidence_target).name + ".rels"))
        if rels_path in zin.namelist():
            sheet_rels_xml = ET.fromstring(zin.read(rels_path))
        else:
            sheet_rels_xml = ET.Element(f"{{{pkg_rel_ns}}}Relationships")

        existing_ids = {rel.attrib.get("Id", "") for rel in sheet_rels_xml}
        next_index = 1
        for ref, url, _label in repaired:
            while f"rIdHyperlink{next_index}" in existing_ids:
                next_index += 1
            rid = f"rIdHyperlink{next_index}"
            existing_ids.add(rid)
            next_index += 1
            ET.SubElement(
                sheet_rels_xml,
                relationship_tag,
                {
                    "Id": rid,
                    "Type": hyperlink_rel_type,
                    "Target": url,
                    "TargetMode": "External",
                },
            )
            ET.SubElement(hyperlinks, f"{{{main_ns}}}hyperlink", {"ref": ref, f"{{{rel_ns}}}id": rid})

        page_margins = sheet_xml.find(f"{{{main_ns}}}pageMargins")
        if page_margins is not None:
            sheet_xml.insert(list(sheet_xml).index(page_margins), hyperlinks)
        else:
            sheet_xml.append(hyperlinks)

        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zout:
                written_rels = False
                for info in zin.infolist():
                    if info.filename == evidence_target:
                        zout.writestr(info, ET.tostring(sheet_xml, encoding="utf-8", xml_declaration=True))
                    elif info.filename == rels_path:
                        zout.writestr(info, ET.tostring(sheet_rels_xml, encoding="utf-8", xml_declaration=True))
                        written_rels = True
                    else:
                        zout.writestr(info, zin.read(info.filename))
                if not written_rels:
                    zout.writestr(rels_path, ET.tostring(sheet_rels_xml, encoding="utf-8", xml_declaration=True))
            shutil.move(str(tmp_path), xlsx_path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()
        refresh_tb_bridge_inspect_hyperlink_labels(xlsx_path)
        return len(repaired)

_INSPECT_HYPERLINK_PLACEHOLDER_RE = re.compile(
    r"^HYPERLINK is not implemented\. linkLocation=(?P<url>.*?)(?:, friendlyName=(?P<label>.*))?$"
)

def _replace_inspect_hyperlink_placeholders(value: Any) -> tuple[Any, int]:
    if isinstance(value, str):
        match = _INSPECT_HYPERLINK_PLACEHOLDER_RE.match(value)
        if not match:
            return value, 0
        label = (match.group("label") or "").strip() or "Click here"
        return label, 1
    if isinstance(value, list):
        replaced_count = 0
        replaced_values = []
        for item in value:
            replaced, count = _replace_inspect_hyperlink_placeholders(item)
            replaced_values.append(replaced)
            replaced_count += count
        return replaced_values, replaced_count
    if isinstance(value, dict):
        replaced_count = 0
        replaced_values: dict[str, Any] = {}
        for key, item in value.items():
            replaced, count = _replace_inspect_hyperlink_placeholders(item)
            replaced_values[key] = replaced
            replaced_count += count
        return replaced_values, replaced_count
    return value, 0

def refresh_tb_bridge_inspect_hyperlink_labels(xlsx_path: Path) -> int:
    """Align artifact-tool inspect output with repaired Excel hyperlinks.

    The workbook repair converts unsupported HYPERLINK formula cells into real
    hyperlink cells with visible "Click here" text. The artifact-tool inspect
    file is generated before that repair, so Turing can otherwise see stale
    placeholder values and report a presentation issue that no longer exists in
    the actual workbook.
    """

    inspect_path = Path(f"{xlsx_path}.inspect.ndjson")
    if not inspect_path.exists():
        return 0
    updated_lines: list[str] = []
    replacement_count = 0
    changed = False
    for line in inspect_path.read_text(errors="ignore").splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            updated_lines.append(line)
            continue
        replaced, count = _replace_inspect_hyperlink_placeholders(payload)
        replacement_count += count
        changed = changed or count > 0
        updated_lines.append(json.dumps(replaced, sort_keys=True))
    if changed:
        inspect_path.write_text("\n".join(updated_lines) + "\n")
    return replacement_count
