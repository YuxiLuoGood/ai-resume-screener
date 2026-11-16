"""
Screening logic with no FastAPI or LangChain dependency: prompts, parsing model output,
verifying evidence, and computing verdicts. Kept separate so it is easy to unit test
(see tests/test_screening.py).
"""
import json
import re
import unicodedata

MAX_CONDITIONS = 6

# ── Prompts ──────────────────────────────────────────────────
# Step 1: split the screening requirement into separate conditions (one call per screening)
SPLIT_PROMPT = """You turn a recruiter's screening requirement into a short list of separate, checkable conditions.

Rules:
- Split on "and", commas and similar connectors. Keep each condition short and faithful to the original wording. Do not add conditions that were not asked for. At most 6.
- If the requirement offers alternatives with "or", keep them together as one condition (for example "Python or Java experience").
- Set "job_related" to false for a condition about a personal characteristic: age, gender, race, ethnicity, nationality, religion, marital or family status, pregnancy, disability, or appearance. Otherwise set it to true.
- Write the conditions in the same language as the requirement.

Reply with JSON only, in exactly this shape:
{"conditions": [{"text": "condition", "job_related": true}]}"""

# Step 2: check one document against every condition
JUDGE_PROMPT = """You check one document (usually a résumé) against a numbered list of conditions. Use only the document text.

For each condition, set "met" to:
- "yes" if the document clearly shows the condition is met. You may compare numbers, add up durations from dates, and accept clear equivalents. Examples: "B.S. in Computer Science" meets "computer science degree"; a GPA of 3.86 meets "GPA above 3.5".
- "no" if the document clearly shows the condition is not met. Example: a GPA of 3.2 does not meet "GPA above 3.5".
- "unclear" if the document does not give enough information. Never guess.

For each condition also give:
- "evidence": a short quote copied exactly from the document that the decision is based on, or "" if there is none.
- "note": a few words explaining the decision, for example "3.86 > 3.5".

Finally, write "summary": one sentence on how well the document fits the conditions overall.
Write "note" and "summary" in the same language as the conditions. Keep "evidence" in the document's own language.

Reply with JSON only, in exactly this shape:
{"results": [{"id": 1, "met": "yes", "evidence": "quote", "note": "why"}], "summary": "one sentence"}"""


def build_judge_message(conditions, context):
    numbered = "\n".join(f"{i}. {c}" for i, c in enumerate(conditions, start=1))
    return f"Conditions:\n{numbered}\n\nDocument:\n{context}"


# ── Text helpers ─────────────────────────────────────────────
def estimate_tokens(text: str) -> int:
    """Rough estimate: ~1 token per CJK character and ~1 token per 4 other characters (errs high)."""
    cjk = sum(1 for ch in text if "\u3040" <= ch <= "\u9fff" or "\uac00" <= ch <= "\ud7af")
    return cjk + (len(text) - cjk) // 4


def format_context(docs) -> str:
    """docs: objects with page_content and metadata['page'] (LangChain Documents)."""
    return "\n\n".join(f"[Page {d.metadata.get('page', '?')}]\n{d.page_content}" for d in docs)


_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-"})


def normalize_for_search(text: str) -> str:
    """Normalize full/half-width forms, case and quotes, and remove all whitespace,
    so we can check whether a quote really appears in the source text."""
    text = unicodedata.normalize("NFKC", text or "").translate(_QUOTES).lower()
    return re.sub(r"\s+", "", text)


def locate_evidence(evidence: str, norm_pages):
    """norm_pages: [(page number, page text after normalize_for_search)]. Return the page number, or None."""
    # Trim punctuation at both ends, including full-width CJK punctuation.
    needle = normalize_for_search(evidence).strip(".,;:!?-*•·\"'()[]，。；：！？、")
    if len(needle) < 3:
        return None
    for page_no, page_text in norm_pages:
        if needle in page_text:
            return page_no
    return None


# ── Parsing model output ─────────────────────────────────────
def parse_json_object(raw):
    """Parse the model's JSON, tolerating ```json fences and extra text around it. Return None on failure."""
    if not isinstance(raw, str):
        return None
    text = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.IGNORECASE | re.MULTILINE).strip()
    for candidate in (text, _first_braced(text)):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _first_braced(text: str):
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if 0 <= start < end else None


def _clean(value, limit: int) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()[:limit]


_TRUE = {"true", "yes", "1"}
_FALSE = {"false", "no", "0"}


def parse_conditions(data, requirement: str):
    """Turn step 1 output into (conditions to check, excluded conditions).
    If parsing fails, the whole requirement becomes a single condition."""
    checked, excluded = [], []
    items = data.get("conditions") if isinstance(data, dict) else None
    if isinstance(items, list):
        for item in items:
            if isinstance(item, str):
                text, related = _clean(item, 200), True
            elif isinstance(item, dict):
                text = _clean(item.get("text") or item.get("condition"), 200)
                flag = item.get("job_related", True)
                related = not (flag is False or str(flag).strip().lower() in _FALSE)
            else:
                continue
            if not text:
                continue
            (checked if related else excluded).append(text)
    checked = list(dict.fromkeys(checked))[:MAX_CONDITIONS]
    excluded = list(dict.fromkeys(excluded))
    if not checked and not excluded:
        checked = [_clean(requirement, 200)]
    return checked, excluded


# Map the model's answer to yes / no / unclear. Chinese labels are accepted too,
# in case the model answers in the language of a Chinese requirement.
_MET = {
    "yes": "yes", "y": "yes", "true": "yes", "met": "yes", "pass": "yes",
    "是": "yes", "满足": "yes", "符合": "yes",
    "no": "no", "n": "no", "false": "no", "not met": "no", "fail": "no",
    "否": "no", "不满足": "no", "不符合": "no",
    "unclear": "unclear", "unknown": "unclear", "not mentioned": "unclear", "n/a": "unclear",
    "不确定": "unclear", "未提及": "unclear", "信息不足": "unclear",
}


def normalize_met(value) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    return _MET.get(str(value or "").strip().lower(), "unclear")


def compute_verdict(rows) -> str:
    """Any condition not met -> no_match; otherwise any unclear -> unclear; all met -> match."""
    mets = [r["met"] for r in rows]
    if not mets:
        return "unclear"
    if "no" in mets:
        return "no_match"
    if "unclear" in mets:
        return "unclear"
    return "match"


def build_result(data, conditions, norm_pages):
    """Align step 2 output to the fixed condition list, verify quotes, and compute the verdict in code."""
    by_id, in_order = {}, []
    items = data.get("results") if isinstance(data, dict) else None
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            in_order.append(item)
            try:
                by_id[int(item.get("id"))] = item
            except (TypeError, ValueError):
                pass

    rows = []
    for i, condition in enumerate(conditions, start=1):
        item = by_id.get(i) or (in_order[i - 1] if not by_id and i <= len(in_order) else {})
        evidence = _clean(item.get("evidence"), 300)
        page = locate_evidence(evidence, norm_pages) if evidence else None
        rows.append(
            {
                "condition": condition,
                "met": normalize_met(item.get("met")),
                "note": _clean(item.get("note"), 200),
                "evidence": evidence,
                "page": page,
                # True: quote found in the source; False: not found (reworded or invented); None: no quote
                "verified": (page is not None) if evidence else None,
            }
        )

    summary = _clean(data.get("summary"), 400) if isinstance(data, dict) else ""
    return {"verdict": compute_verdict(rows), "summary": summary, "conditions": rows}
