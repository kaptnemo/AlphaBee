"""Pipeline utilities shared across orchestrator, harness, and agents.

Provides three low-level helpers that were previously duplicated in every
module that interacts with LLM output:

- ``extract_text``  — normalise LangChain message content to a plain string
- ``parse_json``    — parse a JSON payload from an LLM response, handling
                      markdown fences and partial wrapping robustly
- ``make_id``       — generate a short ``prefix-<hex12>`` ID
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

try:
    from json_repair import repair_json as _repair_json
    _JSON_REPAIR_AVAILABLE = True
except ImportError:
    _JSON_REPAIR_AVAILABLE = False


def extract_text(content: Any) -> str:
    """Normalise LangChain / OpenAI message content to a plain string.

    Handles the three shapes that appear in practice:

    - ``str``         — returned as-is
    - ``list``        — concatenates ``str`` blocks and ``{"type": "text"|"thinking"}``
                        dict blocks, separated by newlines
    - anything else   — coerced via ``str()``
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") in {"text", "thinking"}:
                parts.append(block.get("text", ""))
        return "\n".join(p for p in parts if p)
    return str(content)


def parse_json(text: str) -> Any:
    """Parse a JSON value from an LLM response string.

    Tries multiple candidate extractions in order:

    1. Markdown fenced block (`` ```json … ``` `` or `` ``` … ``` ``)
    2. The raw text itself
    3. The first ``{…}`` or ``[…]`` substring (outermost braces)

    Candidates are deduplicated and tried in order; the first that parses
    successfully is returned.

    Raises:
        ValueError: If none of the candidates parse as valid JSON.
    """
    text = text.strip()
    if not text:
        raise ValueError("LLM returned empty text instead of JSON.")

    candidates: list[str] = []

    # ── 1. Markdown fence (anywhere in text) ─────────────────────────
    import re as _re
    fence_match = _re.search(r"```(?:json)?\s*\n([\s\S]*?)\n```", text)
    if fence_match:
        candidates.append(fence_match.group(1).strip())
    elif text.startswith("```"):
        # Fallback: old line-based extraction for malformed fences
        lines = text.splitlines()
        if len(lines) >= 2:
            fenced = "\n".join(lines[1:-1]).strip()
            if fenced.startswith("json"):
                fenced = fenced[4:].strip()
            candidates.append(fenced)

    # ── 2. Raw text ───────────────────────────────────────────────────
    candidates.append(text)

    # ── 3. First { … } or [ … ] substring ────────────────────────────
    start_positions = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if start_positions:
        start = min(start_positions)
        opener = text[start]
        closer = "}" if opener == "{" else "]"
        end = text.rfind(closer)
        if end > start:
            candidates.append(text[start: end + 1])

    # ── Try each candidate, deduplicating ──────────────────────────────
    seen: set[str] = set()
    for candidate in candidates:
        normalised = candidate.strip()
        if not normalised or normalised in seen:
            continue
        seen.add(normalised)
        try:
            return json.loads(normalised)
        except json.JSONDecodeError:
            continue

    # ── 4. json_repair fallback ───────────────────────────────────────
    if _JSON_REPAIR_AVAILABLE:
        # Try each candidate through the repair engine
        for candidate in candidates:
            normalised = candidate.strip()
            if not normalised:
                continue
            try:
                repaired = _repair_json(normalised, return_objects=True)
                if repaired is not None and repaired != "" and repaired != [] and repaired != {}:
                    return repaired
            except Exception:
                continue

    excerpt = text[:400].replace("\n", "\\n")
    raise ValueError(f"Failed to parse model output as JSON: {excerpt}")


def make_id(prefix: str) -> str:
    """Return a collision-resistant ``prefix-<hex12>`` identifier."""
    return f"{prefix}-{uuid4().hex[:12]}"
