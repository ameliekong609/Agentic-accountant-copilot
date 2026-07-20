"""Compatibility facade for financial statement workflow contracts."""
from __future__ import annotations

from accountant_copilot.accounting_knowledge import (
    ACCOUNTING_PDF_KNOWLEDGE_SKILL,
    ACCOUNTING_PDF_TOPIC_MAP,
    ACCOUNTING_SKILLS_DIR,
    accounting_pdf_retrieval_tool_for_prompt,
    client_evidence_guardrail_for_prompt,
    load_accounting_pdf_topic_map_for_prompt,
    load_accounting_reference_for_prompt,
    load_accounting_skill_for_prompt,
    non_client_evidence_reference_findings,
    source_of_truth_redo_instruction,
    source_of_truth_redo_required,
)
from accountant_copilot.contract_utils import (
    RELATIONSHIP_REASONING_CONTRACT_VERSION,
    TB_BRIDGE_CONTRACT_VERSION,
    TB_BRIDGE_JSON,
    TB_BRIDGE_MD,
    TB_BRIDGE_OUTPUT_DIR,
    TB_BRIDGE_XLSX,
)
from accountant_copilot.relationship_contract import (
    build_relationship_reasoning_prompt,
    failed_relationship_register,
    format_relationship_register,
    normalise_relationship_register,
    relationship_reasoning_context,
    validate_relationship_register,
)
from accountant_copilot.tb_bridge_contract import (
    build_tb_bridge_prompt,
    enrich_tb_bridge_payload_for_workbook,
    failed_tb_bridge_workpaper,
    format_tb_bridge_workpaper,
    matrix_preview,
    normalise_tb_bridge_workpaper,
    relationship_table_items,
    validate_tb_bridge_workpaper,
)
from accountant_copilot.workbook_builder import (
    refresh_tb_bridge_inspect_hyperlink_labels,
    repair_tb_bridge_workbook_hyperlinks,
    write_tb_bridge_workbook_builder,
)
