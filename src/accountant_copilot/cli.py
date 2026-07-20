"""Command-line interface for the Agentic Accountant Copilot."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from accountant_copilot.common import _load_local_env
from accountant_copilot.document_indexing import (
    _build_source_coverage_continuity_payload,
    _classify_raw_document_from_content,
    _codex_document_validation_error,
    _codex_process_document_prompt,
    _document_text_for_codex,
    _normalise_codex_document_result,
    _process_documents_command,
)
from accountant_copilot.relationship_reasoning import (
    SOURCE_MATCHING_CONTRACT_VERSION,
    _codex_source_match_prompt,
    _match_source_facts_command,
    _match_source_facts_from_accounting_command,
    _source_match_context,
)
from accountant_copilot.senior_review import _review_workpaper_command, _validate_turing_review
from accountant_copilot.tb_bridge_runner import (
    COA_MAPPING_CONTRACT_VERSION,
    _apply_turing_corrections_command,
    _build_coa_mapping_workpaper_command,
    _validate_coa_mapping_workpaper,
)
from accountant_copilot.contract_utils import TB_BRIDGE_JSON, TB_BRIDGE_OUTPUT_DIR, TB_BRIDGE_XLSX
from accountant_copilot.workpaper_orchestrator import prepare_workpaper_command


def _serve_workpaper_portal_command(args: argparse.Namespace) -> int:
    from accountant_copilot.workpaper_portal import serve_workpaper_portal

    serve_workpaper_portal(repo_root=Path.cwd(), host=args.host, port=args.port)
    return 0


def _prepare_workpaper_command(args: argparse.Namespace) -> int:
    return prepare_workpaper_command(
        args,
        process_documents_command=_process_documents_command,
        match_source_facts_command=_match_source_facts_command,
        build_coa_mapping_workpaper_command=_build_coa_mapping_workpaper_command,
        review_workpaper_command=_review_workpaper_command,
        apply_turing_corrections_command=_apply_turing_corrections_command,
    )

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="accountant-copilot",
        description="Prepare an AI-assisted financial statement workpaper from a client document pack.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    process_documents_parser = subparsers.add_parser(
        "process-documents",
        help="Build the source evidence index from an uploaded document folder.",
    )
    process_documents_parser.add_argument("--input-dir", default="inputs")
    process_documents_parser.add_argument("--artifact-dir", default="outputs/raw_inputs_pdf_extraction")
    process_documents_parser.add_argument("--codex-command", default="codex exec")
    process_documents_parser.add_argument("--codex-timeout", default=120, type=int)
    process_documents_parser.add_argument("--codex-max-attempts", default=3, type=int)
    process_documents_parser.add_argument("--batch-size", default=5, type=int)
    process_documents_parser.add_argument(
        "--force-reprocess",
        action="store_true",
        help="Ignore existing per-document cache and reread uploaded files.",
    )
    process_documents_parser.set_defaults(func=_process_documents_command)

    source_fact_match_parser = subparsers.add_parser(
        "match-source-facts",
        help="Build the relationship reasoning register from the source evidence index.",
    )
    source_fact_match_parser.add_argument("--accounting-facts", default=None)
    source_fact_match_parser.add_argument("--source-coverage", default=None)
    source_fact_match_parser.add_argument("--codex-command", default="codex exec")
    source_fact_match_parser.add_argument("--codex-timeout", type=int, default=600)
    source_fact_match_parser.add_argument("--codex-max-attempts", type=int, default=3)
    source_fact_match_parser.add_argument("--bank-transactions", default=None, help=argparse.SUPPRESS)
    source_fact_match_parser.add_argument("--invoice-facts", default=None, help=argparse.SUPPRESS)
    source_fact_match_parser.add_argument("--distribution-tax-facts", default=None, help=argparse.SUPPRESS)
    source_fact_match_parser.add_argument("--broker-trade-facts", default=None, help=argparse.SUPPRESS)
    source_fact_match_parser.add_argument("--output", default="outputs/source_fact_matches.md")
    source_fact_match_parser.set_defaults(func=_match_source_facts_command)

    step4_workpaper_parser = subparsers.add_parser(
        "build-tb-bridge-workpaper",
        help="Build the accountant-style TB bridge workbook from relationship reasoning.",
    )
    step4_workpaper_parser.add_argument("--artifact-dir", default="outputs/raw_inputs_pdf_extraction")
    step4_workpaper_parser.add_argument("--output-dir", default=TB_BRIDGE_OUTPUT_DIR)
    step4_workpaper_parser.add_argument("--event-register", default=None)
    step4_workpaper_parser.add_argument("--source-index", default=None)
    step4_workpaper_parser.add_argument("--prior-coa", default=None)
    step4_workpaper_parser.add_argument(
        "--prior-fs-document-id",
        default=None,
        help="Document id of the single prior-year financial statement to use as opening balances.",
    )
    step4_workpaper_parser.add_argument(
        "--prior-fs-file",
        default=None,
        help="File name/path of the single prior-year financial statement to use as opening balances.",
    )
    step4_workpaper_parser.add_argument("--codex-command", default="codex exec")
    step4_workpaper_parser.add_argument("--codex-timeout", type=int, default=600)
    step4_workpaper_parser.add_argument("--codex-max-attempts", type=int, default=3)
    step4_workpaper_parser.add_argument("--skip-xlsx", action="store_true")
    step4_workpaper_parser.set_defaults(func=_build_coa_mapping_workpaper_command)

    prepare_workpaper_parser = subparsers.add_parser(
        "prepare-workpaper",
        help="Run the full financial statement workpaper preparation workflow.",
    )
    prepare_workpaper_parser.add_argument("--client-folder", required=True, help="Folder containing source documents for the workpaper.")
    prepare_workpaper_parser.add_argument("--artifact-dir", default="outputs/raw_inputs_pdf_extraction")
    prepare_workpaper_parser.add_argument("--output-dir", default=TB_BRIDGE_OUTPUT_DIR)
    prepare_workpaper_parser.add_argument("--entity-name", default=None)
    prepare_workpaper_parser.add_argument("--fy-start", default=None, help="Target financial year start date, e.g. 2024-07-01.")
    prepare_workpaper_parser.add_argument("--fy-end", default=None, help="Target financial year end date, e.g. 2025-06-30.")
    prepare_workpaper_parser.add_argument("--prior-fs-document-id", default=None, help="Document id of the single prior-year financial statement to use as opening balances.")
    prepare_workpaper_parser.add_argument("--prior-fs-file", default=None, help="File name/path of the single prior-year financial statement to use as opening balances.")
    prepare_workpaper_parser.add_argument("--codex-command", default="codex exec")
    prepare_workpaper_parser.add_argument("--codex-timeout", type=int, default=1200)
    prepare_workpaper_parser.add_argument("--codex-max-attempts", type=int, default=3)
    prepare_workpaper_parser.add_argument("--batch-size", type=int, default=5)
    prepare_workpaper_parser.add_argument("--review-sample-size", type=int, default=8)
    prepare_workpaper_parser.add_argument(
        "--review-correction-rounds",
        type=int,
        default=2,
        help="Maximum bounded senior-review correction rounds before stopping for human attention.",
    )
    prepare_workpaper_parser.add_argument(
        "--force-reprocess",
        action="store_true",
        help="Ignore existing per-document cache. prepare-workpaper is fresh by default unless --allow-cache is supplied.",
    )
    prepare_workpaper_parser.add_argument("--allow-cache", action="store_true", help="Allow source-index cache reuse for a faster development run.")
    prepare_workpaper_parser.add_argument("--skip-xlsx", action="store_true")
    prepare_workpaper_parser.add_argument("--skip-review", action="store_true")
    prepare_workpaper_parser.set_defaults(func=_prepare_workpaper_command)

    review_workpaper_parser = subparsers.add_parser(
        "review-workpaper",
        help="Run the senior accountant review over a prepared TB bridge workbook.",
    )
    review_workpaper_parser.add_argument("--client-folder", default=None)
    review_workpaper_parser.add_argument("--artifact-dir", default="outputs/raw_inputs_pdf_extraction")
    review_workpaper_parser.add_argument("--output-dir", default=TB_BRIDGE_OUTPUT_DIR)
    review_workpaper_parser.add_argument("--workpaper-json", default=None)
    review_workpaper_parser.add_argument("--source-index", default=None)
    review_workpaper_parser.add_argument("--event-register", default=None)
    review_workpaper_parser.add_argument("--prior-coa", default=None)
    review_workpaper_parser.add_argument("--output", default=None)
    review_workpaper_parser.add_argument("--entity-name", default=None)
    review_workpaper_parser.add_argument("--codex-command", default="codex exec")
    review_workpaper_parser.add_argument("--codex-timeout", type=int, default=1200)
    review_workpaper_parser.add_argument("--codex-max-attempts", type=int, default=3)
    review_workpaper_parser.add_argument("--sample-size", type=int, default=8)
    review_workpaper_parser.set_defaults(func=_review_workpaper_command)

    workpaper_portal_parser = subparsers.add_parser(
        "serve-workpaper-portal",
        help="Start the local accountant-facing workpaper portal.",
        description="Start the local accountant-facing workpaper portal.",
    )
    workpaper_portal_parser.add_argument("--host", default="127.0.0.1")
    workpaper_portal_parser.add_argument("--port", default=8787, type=int)
    workpaper_portal_parser.set_defaults(func=_serve_workpaper_portal_command)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _load_local_env()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
