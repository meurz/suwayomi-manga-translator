"""Persist aggregate language evidence without retaining OCR dialogue."""

import json

COUNTS = ("zh_regions", "zh_chars", "foreign_regions", "uncertain_regions")


def empty_evidence():
    return {"version": 1, **dict.fromkeys(COUNTS, 0)}


def parse_evidence(value):
    try:
        value = json.loads(value) if isinstance(value, str) else value
        if not isinstance(value, dict) or value.get("version") != 1:
            return {}
        if any(type(value.get(k)) is not int or not 0 <= value[k] <= 1_000_000 for k in COUNTS):
            return {}
        return {"version": 1, **{k: value[k] for k in COUNTS}}
    except (ValueError, TypeError):
        return {}


def summarize_languages(texts, decisions):
    evidence = empty_evidence()
    for text, decision in zip(texts, decisions, strict=True):
        if not any(c.isalpha() for c in text):
            continue
        language = decision.language.lower().split("-")[0]
        han = sum("\u3400" <= c <= "\u9fff" or "\U00020000" <= c <= "\U0003134f" for c in text)
        if language == "zh" and decision.confident and han:
            evidence["zh_regions"] += 1
            evidence["zh_chars"] += han
        elif language in {"ja", "en", "ko"}:
            evidence["foreign_regions"] += 1
        else:
            evidence["uncertain_regions"] += 1
    return evidence


def chinese_chapter(pages):
    """Require a complete chapter and several distinct text-bearing pages."""
    unique = {page["original_sha"]: page for page in pages}.values()
    evidence = [parse_evidence(page["evidence"]) for page in unique]
    if not evidence or any(not e for e in evidence):
        return None
    if any(e["foreign_regions"] or e["uncertain_regions"] for e in evidence):
        return None
    text_pages = sum(e["zh_regions"] > 0 for e in evidence)
    regions = sum(e["zh_regions"] for e in evidence)
    chars = sum(e["zh_chars"] for e in evidence)
    if text_pages < 3 or regions < 6 or chars < 80:
        return None
    return {"text_pages": text_pages, "zh_regions": regions, "zh_chars": chars}
