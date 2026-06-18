from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args: str):
    return subprocess.run(
        [sys.executable, "-m", "accountant_copilot.cli", *args],
        cwd=ROOT,
        env={"PYTHONPATH": "src"},
        text=True,
        capture_output=True,
        check=False,
    )


def write_text_pdf(path: Path, text: str) -> None:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n",
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >> endobj\n",
        b"4 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n",
        b"5 0 obj << /Length " + str(len(stream)).encode() + b" >> stream\n" + stream + b"\nendstream endobj\n",
    ]
    content = b"%PDF-1.4\n"
    offsets = []
    for obj in objects:
        offsets.append(len(content))
        content += obj
    xref_at = len(content)
    content += f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        content += f"{offset:010d} 00000 n \n".encode()
    content += f"trailer << /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode()
    path.write_bytes(content)


def test_setup_turing_workspace_exports_review_artifacts_and_summary(tmp_path: Path) -> None:
    input_dir = tmp_path / "inputs"
    output_dir = tmp_path / "setup"
    input_dir.mkdir()
    write_text_pdf(
        input_dir / "eStatement.pdf",
        "Statement Period 1 Jan 2025 - 31 Jan 2025 Opening Balance $30,211.09 CR Total Credits + $10,080.31 Total Debits - $9,966.39 Closing Balance $30,325.01 CR Business Transaction Account",
    )
    (input_dir / "client_conventions.md").write_text("Use source-linked review before release.")

    result = run_cli(
        "setup-turing-workspace",
        "--input-dir", str(input_dir),
        "--output-dir", str(output_dir),
        "--entity-name", "Fixture Trust",
    )

    assert result.returncode == 1
    assert (output_dir / "engagement_state.json").exists()
    assert (output_dir / "review_packet" / "README.md").exists()
    assert (output_dir / "review.html").exists()
    assert (output_dir / "local_ui" / "index.html").exists()
    assert (output_dir / "statement_package" / "verifier_result.json").exists()
    assert (output_dir / "document_inventory.md").exists()
    assert (output_dir / "bank_statement_facts.json").exists()
    assert (output_dir / "bank_continuity.json").exists()
    assert (output_dir / "bank_transactions.json").exists()
    assert (output_dir / "invoice_facts.json").exists()
    assert (output_dir / "invoice_review.json").exists()
    assert (output_dir / "distribution_tax_facts.json").exists()
    summary = (output_dir / "SETUP_RESULTS.md").read_text()
    assert "Turing Financial Statement Automation Setup" in summary
    assert "Final output allowed: YES" in summary
    assert "Setup review steps with findings:" in summary
    payload = json.loads((output_dir / "document_inventory.json").read_text())
    assert len(payload["documents"]) == 2
