"""Accounting knowhow loading and source-of-truth guardrails."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from accountant_copilot.contract_utils import _as_list

ACCOUNTING_SKILLS_DIR = Path(__file__).resolve().parents[2] / "knowhow" / "skills"

ACCOUNTING_PDF_KNOWLEDGE_SKILL = "accounting-pdf-knowledge-retrieval"

ACCOUNTING_PDF_TOPIC_MAP = "pdf-topic-map.json"

def load_accounting_skill_for_prompt(skill_name: str, *, char_limit: int = 9000) -> dict[str, str]:
    """Load a repo-contained accountant skill for Codex prompts.

    Skills guide reasoning only. They are never client evidence.
    """
    safe_name = re.sub(r"[^a-z0-9-]", "", str(skill_name or "").casefold())
    path = ACCOUNTING_SKILLS_DIR / safe_name / "SKILL.md"
    if not safe_name or not path.exists():
        return {
            "name": safe_name or skill_name,
            "status": "missing",
            "source": "repo_knowhow_skill",
            "body": "",
            "rule": "Missing skills do not block the workflow.",
        }
    raw = path.read_text(encoding="utf-8", errors="ignore")
    description = ""
    body = raw
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) == 3:
            frontmatter, body = parts[1], parts[2]
            for line in frontmatter.splitlines():
                if line.strip().startswith("description:"):
                    description = line.split(":", 1)[1].strip().strip("\"'")
                    break
    return {
        "name": safe_name,
        "description": description,
        "status": "loaded",
        "source": "repo_knowhow_skill",
        "body": body.strip()[:char_limit],
        "rule": "Use this as accounting judgement guidance only. Do not cite it as client evidence.",
    }

def load_accounting_reference_for_prompt(skill_name: str, reference_name: str, *, char_limit: int = 8000) -> dict[str, str]:
    safe_skill_name = re.sub(r"[^a-z0-9-]", "", str(skill_name or "").casefold())
    safe_reference = re.sub(r"[^a-z0-9_.-]", "", str(reference_name or "").casefold())
    path = ACCOUNTING_SKILLS_DIR / safe_skill_name / "references" / safe_reference
    if not safe_skill_name or not safe_reference or not path.exists():
        return {
            "skill": safe_skill_name or skill_name,
            "name": safe_reference or reference_name,
            "status": "missing",
            "source": "repo_knowhow_reference",
            "body": "",
            "rule": "Missing references do not block the workflow.",
        }
    return {
        "skill": safe_skill_name,
        "name": safe_reference,
        "status": "loaded",
        "source": "repo_knowhow_reference",
        "body": path.read_text(encoding="utf-8", errors="ignore").strip()[:char_limit],
        "rule": "Use this as accounting judgement guidance only. Do not cite it as client evidence.",
    }

def load_accounting_pdf_topic_map_for_prompt(*, char_limit: int = 14000) -> dict[str, Any]:
    reference = load_accounting_reference_for_prompt(ACCOUNTING_PDF_KNOWLEDGE_SKILL, ACCOUNTING_PDF_TOPIC_MAP, char_limit=char_limit)
    if reference.get("status") != "loaded":
        return {
            "status": reference.get("status", "missing"),
            "source": "repo_knowhow_pdf_topic_map",
            "topics": [],
            "rule": "Missing PDF topic map does not block the workflow.",
        }
    try:
        topic_map = json.loads(reference.get("body", "{}"))
    except json.JSONDecodeError:
        return {
            "status": "invalid_json",
            "source": "repo_knowhow_pdf_topic_map",
            "topics": [],
            "rule": "Invalid PDF topic map does not block the workflow.",
        }
    topics = []
    for topic in _as_list(topic_map.get("topics")):
        if not isinstance(topic, dict):
            continue
        topics.append(
            {
                "topic_id": topic.get("topic_id", ""),
                "label": topic.get("label", ""),
                "use_when": topic.get("use_when", ""),
                "triggers": _as_list(topic.get("triggers"))[:12],
                "sections": [
                    {
                        "document": section.get("document", ""),
                        "section": section.get("section", ""),
                        "pdf_pages": section.get("pdf_pages", []),
                        "search_terms": _as_list(section.get("search_terms"))[:8],
                    }
                    for section in _as_list(topic.get("sections"))
                    if isinstance(section, dict)
                ],
            }
        )
    return {
        "status": "loaded",
        "source": "repo_knowhow_pdf_topic_map",
        "map_version": topic_map.get("map_version", ""),
        "documents": topic_map.get("documents", {}),
        "topics": topics,
        "rule": topic_map.get("source_of_truth_rule", "PDF knowhow is judgement guidance only; client evidence remains source of truth."),
    }

def accounting_pdf_retrieval_tool_for_prompt() -> dict[str, Any]:
    return {
        "skill": ACCOUNTING_PDF_KNOWLEDGE_SKILL,
        "map": f"knowhow/skills/{ACCOUNTING_PDF_KNOWLEDGE_SKILL}/references/{ACCOUNTING_PDF_TOPIC_MAP}",
        "script": f"knowhow/skills/{ACCOUNTING_PDF_KNOWLEDGE_SKILL}/scripts/retrieve_pdf_topic.py",
        "list_topics_command": (
            f"PYTHONPATH=src .venv/bin/python knowhow/skills/{ACCOUNTING_PDF_KNOWLEDGE_SKILL}/scripts/retrieve_pdf_topic.py --list-topics"
        ),
        "retrieve_topic_command_template": (
            f"PYTHONPATH=src .venv/bin/python knowhow/skills/{ACCOUNTING_PDF_KNOWLEDGE_SKILL}/scripts/retrieve_pdf_topic.py --topic <topic_id>"
        ),
        "retrieve_query_command_template": (
            f"PYTHONPATH=src .venv/bin/python knowhow/skills/{ACCOUNTING_PDF_KNOWLEDGE_SKILL}/scripts/retrieve_pdf_topic.py --query \"<technical accounting question>\""
        ),
        "rule": (
            "Use this only as an on-demand accounting book consultation. Do not cite retrieved knowhow PDFs as client evidence; "
            "use the retrieved section to decide what client evidence to inspect and how to classify judgement."
        ),
    }

def client_evidence_guardrail_for_prompt() -> dict[str, Any]:
    return {
        "source_of_truth": [
            "Uploaded client source documents in source_documents/source_document_index.",
            "The selected prior-year financial statements for opening balances and account structure.",
            "Bank statements, invoices, investment statements, tax statements, broker confirmations, capital calls, and other client files.",
        ],
        "allowed_skill_use": [
            "Use accounting skills, CA knowhow PDFs, and retrieved PDF snippets only to guide judgement, classification, review focus, and explanation style.",
            "Use skills to decide what to check, not as evidence that an amount, date, account, counterparty, or treatment exists for this client.",
        ],
        "forbidden_skill_use": [
            "Do not cite knowhow PDFs, retrieved PDF snippets, SKILL.md files, training material, or accounting skills as evidence.",
            "Do not put knowhow/skill files into document_refs, evidence_nodes, source_documents_checked, evidence_summary, or movement notes as support.",
            "Do not infer a client-specific amount, date, account, counterparty, or final treatment solely from a skill or training document.",
        ],
        "if_uncertain": "Mark the item needs_attention or unsupported and say which client evidence is missing.",
    }

_NON_CLIENT_EVIDENCE_TERMS = [
    "knowhow/skills",
    "knowhow\\skills",
    "skill.md",
    "accounting skill",
    "tb-bridge-preparation",
    "accounting-relationship-reasoning",
    "senior-workpaper-review",
    "accounting-pdf-knowledge-retrieval",
    "pdf-topic-map",
    "fin121_csg",
    "maaf121_csg",
    "knowhow/fin121",
    "knowhow/maaf121",
    "fin121 unit",
    "maaf121",
    "candidate study guide",
    "training material",
]

_NON_CLIENT_NEGATED_REFERENCE_TERMS = [
    "no skill",
    "no knowhow",
    "no training material",
    "not cited",
    "not cite",
    "not used as evidence",
    "not used as client evidence",
    "does not cite",
    "does not use",
    "without citing",
    "without using",
]

def _non_client_reference_is_negated(value: str) -> bool:
    lowered = value.casefold()
    if not any(term in lowered for term in _NON_CLIENT_NEGATED_REFERENCE_TERMS):
        return False
    if any(
        phrase in lowered
        for phrase in (
            "used as evidence",
            "cited as evidence",
            "relied on as evidence",
            "source documents checked",
            "evidence_summary: knowhow",
        )
    ) and not any(negated in lowered for negated in ("not used as evidence", "not cited", "does not cite", "without citing")):
        return False
    return True

def _non_client_evidence_hits(value: Any, *, path: str = "$") -> list[dict[str, str]]:
    hits: list[dict[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            hits.extend(_non_client_evidence_hits(item, path=f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            hits.extend(_non_client_evidence_hits(item, path=f"{path}[{index}]"))
    elif isinstance(value, str):
        lowered = value.casefold()
        if _non_client_reference_is_negated(value):
            return hits
        for term in _NON_CLIENT_EVIDENCE_TERMS:
            if term in lowered:
                hits.append({"path": path, "term": term, "value": value[:240]})
                break
    return hits

def non_client_evidence_reference_findings(value: Any, *, stage: str, message: str) -> list[dict[str, str]]:
    return [
        {
            "category": "non_client_evidence_reference",
            "severity": "high",
            "redo_required": True,
            "message": message,
            "stage": stage,
            "path": hit["path"],
            "term": hit["term"],
            "value": hit["value"],
        }
        for hit in _non_client_evidence_hits(value)[:8]
    ]

def source_of_truth_redo_required(findings: list[dict[str, Any]] | None) -> bool:
    return any(isinstance(finding, dict) and finding.get("category") == "non_client_evidence_reference" for finding in _as_list(findings))

def source_of_truth_redo_instruction(findings: list[dict[str, Any]] | None) -> str:
    if not source_of_truth_redo_required(findings):
        return ""
    return (
        "Redo the output from the affected stage. The previous output used accounting skills, knowhow PDFs, retrieved book snippets, training material, or SKILL.md text as evidence. "
        "Discard those citations. Use skills and retrieved PDF sections only as judgement guidance. Replace support with uploaded client documents, prior-year FS, bank statements, "
        "source document page quotes, or derived arithmetic from client evidence. If client evidence is missing, mark the item needs_attention or unsupported."
    )
