#!/usr/bin/env python3
"""Retrieve targeted snippets from local accounting knowhow PDFs."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


SCRIPT_PATH = Path(__file__).resolve()
SKILL_DIR = SCRIPT_PATH.parents[1]
REPO_ROOT = SCRIPT_PATH.parents[4]
DEFAULT_MAP_PATH = SKILL_DIR / "references" / "pdf-topic-map.json"


def _load_map(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _topic_score(topic: dict[str, Any], query: str) -> int:
    haystack = " ".join(
        [
            str(topic.get("topic_id", "")),
            str(topic.get("label", "")),
            str(topic.get("use_when", "")),
            " ".join(str(item) for item in topic.get("triggers", [])),
        ]
    ).casefold()
    query_terms = [term for term in _normalise(query).split() if len(term) > 2]
    return sum(1 for term in query_terms if term in haystack)


def _select_topics(topic_map: dict[str, Any], *, topic_id: str | None, query: str | None, limit: int) -> list[dict[str, Any]]:
    topics = [topic for topic in topic_map.get("topics", []) if isinstance(topic, dict)]
    if topic_id:
        wanted = _normalise(topic_id)
        selected = [
            topic
            for topic in topics
            if _normalise(str(topic.get("topic_id", ""))) == wanted or _normalise(str(topic.get("label", ""))) == wanted
        ]
        if selected:
            return selected[:limit]
        return []
    if query:
        ranked = sorted(((topic, _topic_score(topic, query)) for topic in topics), key=lambda item: item[1], reverse=True)
        return [topic for topic, score in ranked if score > 0][:limit]
    return []


def _page_text(pdf_path: Path, page_number: int) -> str:
    try:
        import fitz  # type: ignore
    except Exception as exc:  # pragma: no cover - environment guard
        raise RuntimeError("PyMuPDF is required to extract knowhow PDF text.") from exc
    with fitz.open(pdf_path) as doc:
        if page_number < 1 or page_number > len(doc):
            return ""
        return doc[page_number - 1].get_text("text")


def _context_snippets(text: str, terms: list[str], *, max_lines: int = 10) -> list[str]:
    lines = [" ".join(line.split()) for line in text.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return []
    lowered_terms = [term.casefold() for term in terms if term]
    snippets: list[str] = []
    seen: set[int] = set()
    for index, line in enumerate(lines):
        lowered = line.casefold()
        if lowered_terms and not any(term in lowered for term in lowered_terms):
            continue
        start = max(0, index - 1)
        end = min(len(lines), index + 2)
        key = start * 10000 + end
        if key in seen:
            continue
        seen.add(key)
        snippets.append(" ".join(lines[start:end]))
        if len(snippets) >= max_lines:
            break
    if snippets:
        return snippets
    return [" ".join(lines[:8])]


def _extract_topic(topic_map: dict[str, Any], topic: dict[str, Any], *, context_pages: int, max_chars: int) -> dict[str, Any]:
    documents = topic_map.get("documents", {})
    result_sections: list[dict[str, Any]] = []
    remaining = max_chars
    for section in topic.get("sections", []):
        if remaining <= 0 or not isinstance(section, dict):
            break
        document_key = str(section.get("document", ""))
        document = documents.get(document_key, {}) if isinstance(documents, dict) else {}
        pdf_path = REPO_ROOT / str(document.get("path", ""))
        page_range = section.get("pdf_pages", [])
        if not pdf_path.exists() or not isinstance(page_range, list) or len(page_range) != 2:
            result_sections.append(
                {
                    "document": document_key,
                    "section": section.get("section", ""),
                    "status": "missing_pdf_or_page_range",
                    "pdf_path": str(pdf_path),
                }
            )
            continue
        start, end = int(page_range[0]), int(page_range[1])
        pages = range(max(1, start - context_pages), end + context_pages + 1)
        search_terms = [str(term) for term in section.get("search_terms", [])]
        section_snippets: list[dict[str, Any]] = []
        for page in pages:
            if remaining <= 0:
                break
            text = _page_text(pdf_path, page)
            if not text.strip():
                continue
            snippets = _context_snippets(text, search_terms, max_lines=3)
            for snippet in snippets:
                if remaining <= 0:
                    break
                clipped = snippet[: min(len(snippet), remaining)]
                if clipped:
                    section_snippets.append(
                        {
                            "pdf_page": page,
                            "matched_terms": [term for term in search_terms if term.casefold() in snippet.casefold()][:5],
                            "text": clipped,
                        }
                    )
                    remaining -= len(clipped)
        result_sections.append(
            {
                "document": document_key,
                "title": document.get("title", ""),
                "pdf_path": str(pdf_path.relative_to(REPO_ROOT)) if pdf_path.exists() else str(pdf_path),
                "section": section.get("section", ""),
                "mapped_pdf_pages": page_range,
                "search_terms": search_terms,
                "snippets": section_snippets,
            }
        )
    return {
        "topic_id": topic.get("topic_id"),
        "label": topic.get("label"),
        "use_when": topic.get("use_when"),
        "source_of_truth_rule": topic_map.get("source_of_truth_rule"),
        "sections": result_sections,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Retrieve original PDF snippets for accounting knowhow topics.")
    parser.add_argument("--map", default=str(DEFAULT_MAP_PATH), help="Path to pdf-topic-map.json.")
    parser.add_argument("--list-topics", action="store_true", help="List available topic IDs.")
    parser.add_argument("--topic", help="Topic ID or exact label to retrieve.")
    parser.add_argument("--query", help="Free-text query used to select likely topics.")
    parser.add_argument("--limit", type=int, default=2, help="Maximum topics to retrieve for a query.")
    parser.add_argument("--context-pages", type=int, default=0, help="Extra pages around mapped page ranges.")
    parser.add_argument("--max-chars", type=int, default=12000, help="Maximum snippet characters to return.")
    args = parser.parse_args()

    topic_map = _load_map(Path(args.map))
    topics = [topic for topic in topic_map.get("topics", []) if isinstance(topic, dict)]
    if args.list_topics:
        print(
            json.dumps(
                {
                    "map_version": topic_map.get("map_version"),
                    "topics": [
                        {
                            "topic_id": topic.get("topic_id"),
                            "label": topic.get("label"),
                            "triggers": topic.get("triggers", [])[:8],
                        }
                        for topic in topics
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    selected = _select_topics(topic_map, topic_id=args.topic, query=args.query, limit=max(1, args.limit))
    if not selected:
        print(json.dumps({"status": "no_topic_matched", "query": args.query or args.topic or ""}, indent=2, sort_keys=True))
        return 2

    payload = {
        "artifact_type": "accounting_pdf_knowledge_retrieval",
        "map_version": topic_map.get("map_version"),
        "guidance_only": True,
        "client_evidence_warning": topic_map.get("source_of_truth_rule"),
        "topics": [
            _extract_topic(topic_map, topic, context_pages=max(0, args.context_pages), max_chars=max(1000, args.max_chars // len(selected)))
            for topic in selected
        ],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
