from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

from accountant_copilot.state.engagement import EngagementState

ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args: str):
    env = os.environ.copy()
    env["PYTHONPATH"] = "src"
    return subprocess.run([sys.executable, "-m", "accountant_copilot.cli", *args], cwd=ROOT, env=env, text=True, capture_output=True, check=False)


def write_state(path: Path) -> None:
    path.write_text(EngagementState(engagement_id="raw_test", entity_name="Raw Trust", entity_type="discretionary_trust", fy_start="2024-07-01", fy_end="2025-06-30", documents_ref="inputs", coa_ref="inputs/prior.pdf").model_dump_json())


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


def test_ingest_raw_inputs_extracts_text_pdf_as_page_evidence(tmp_path: Path):
    input_dir = tmp_path / "inputs"
    input_dir.mkdir()
    write_text_pdf(input_dir / "bank.pdf", "Bank closing balance 123.45")
    state = tmp_path / "state.json"
    write_state(state)

    result = run_cli("ingest-raw-inputs", "--state", str(state), "--input-dir", str(input_dir))

    assert result.returncode == 0
    data = json.loads(state.read_text())
    assert len(data["source_documents"]) == 1
    assert data["exceptions"] == []
    evidence = data["evidence"][0]
    assert evidence["page"] == "1"
    assert evidence["document_id"] == "raw_001"
    assert "Bank closing balance 123.45" in evidence["quote"]


def test_ingest_raw_inputs_registers_documents_and_blocks_unextracted_sources(tmp_path: Path):
    input_dir = tmp_path / "inputs"
    input_dir.mkdir()
    (input_dir / "bank.pdf").write_bytes(b"%PDF-1.4\nraw bank statement")
    (input_dir / "conventions.md").write_text("Use internal conventions.")
    state = tmp_path / "state.json"
    write_state(state)

    result = run_cli("ingest-raw-inputs", "--state", str(state), "--input-dir", str(input_dir))

    assert result.returncode == 1
    assert "extraction-required" in result.stdout
    data = json.loads(state.read_text())
    assert len(data["source_documents"]) == 2
    assert len(data["evidence"]) == 1
    assert data["evidence"][0]["source_type"] == "client_conventions"
    exceptions = {item["category"]: item for item in data["exceptions"]}
    assert "source_extraction_required" in exceptions
    assert exceptions["source_extraction_required"]["severity"] == "high"

    inspected = run_cli("inspect-engagement", "--state", str(state), "--json")
    payload = json.loads(inspected.stdout)
    assert inspected.returncode == 1
    assert payload["final_output_allowed"] is False
    assert payload["blocking_exception_count"] >= 1


def test_ingest_raw_inputs_records_image_ocr_evidence_when_available(tmp_path: Path, monkeypatch):
    input_dir = tmp_path / "inputs"
    input_dir.mkdir()
    image = input_dir / "invoice.png"
    image.write_bytes(b"not-a-real-image-but-fake-tesseract-ignores-it")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_tesseract = fake_bin / "tesseract"
    fake_tesseract.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' 'TAX INVOICE Invoice Number INV-0082 Amount Due AUD 1,100.00'\n"
    )
    fake_tesseract.chmod(fake_tesseract.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}")
    state = tmp_path / "state.json"
    write_state(state)

    result = run_cli("ingest-raw-inputs", "--state", str(state), "--input-dir", str(input_dir))

    assert result.returncode == 0
    data = json.loads(state.read_text())
    assert data["exceptions"] == []
    evidence = data["evidence"][0]
    assert evidence["source_type"] == "image_support"
    assert evidence["page"] == "1"
    assert evidence["confidence"] == "image_ocr"
    assert "INV-0082" in evidence["quote"]
    assert "1,100.00" in evidence["quote"]


def test_run_engagement_from_raw_input_dir_exports_review_packet(tmp_path: Path):
    input_dir = tmp_path / "inputs"
    input_dir.mkdir()
    (input_dir / "eStatement.pdf").write_bytes(b"%PDF-1.4\nbank")
    state = tmp_path / "state.json"
    packet = tmp_path / "review_packet"
    ui = tmp_path / "review.html"
    write_state(state)

    result = run_cli("run-engagement", "--state", str(state), "--input-dir", str(input_dir), "--review-packet-dir", str(packet), "--review-ui", str(ui))

    assert result.returncode == 1
    assert "Engagement blocked" in result.stdout
    assert (packet / "open_exceptions.md").exists()
    assert ui.exists()
    open_text = (packet / "open_exceptions.md").read_text()
    assert "source_extraction_required" in open_text
