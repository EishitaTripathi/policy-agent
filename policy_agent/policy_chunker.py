"""
COMPONENT: policy_chunker
DESIGN-REF: D4 (Policy retrieval — section-aware chunking)
PURPOSE: Parse the expanded policy markdown into per-clause chunks with
  metadata (section_id, parent_section, section_title, action_verb,
  cross_refs, is_seed). Used by ingest to populate ChromaDB and by the
  agent's citation flow to verify retrieved chunks.
PROBLEM-STATEMENT REQ (verbatim): >
  "Policy & Rule Representation — With a 15-30 page policy document, how
  you store, index, and retrieve rules at runtime matters. Naive chunking
  will lose cross-references between sections."
EXPECTED INPUT: markdown text of the full policy bundle (seed + expanded)
EXPECTED OUTPUT: list[Clause] — one Clause per leaf clause (e.g. 7.1, 7.2)
UPSTREAM: policy_agent.ingest, policy_agent.retrieval (for reload), tests
DOWNSTREAM: stdlib only (re, dataclasses) — no model dependencies
COMPONENT TESTS: tests/whitebox/test_policy_chunker.py
SCENARIO COVERAGE: foundation for all 21 (every scenario retrieves chunks)

Format heuristics (the expanded policy uses several styles):
  - inline:  "X.Y. <body>"  or  "X.Y <body>"
  - H3:      "### X.Y <title>\\n<body>"  or  "### X.Y. <title>\\n<body>"
The chunker accepts all four, normalizes to a single Clause shape.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Section heading: "## Section <N> — <Title>" (em-dash, en-dash, or hyphen).
SECTION_HEADING = re.compile(
    r"^## Section (\d+)\s*[—–-]\s*(.+?)\s*$",
    re.MULTILINE,
)

# Cross-references: "§X.Y", "§X", "Section X" — used inside clause bodies.
CROSS_REF = re.compile(r"§\s*(\d+(?:\.\d+(?:\.\d+)?)?)")
SECTION_REF = re.compile(r"\bSection\s+(\d+(?:\.\d+)?)\b")

# Action verbs (case-insensitive, in priority order: most-restrictive first).
# We check on a normalized body that strips Markdown bold markers so
# "**must not**" is detected as "must not".
_VERB_ORDER = ("must not", "must", "should", "may")


@dataclass
class Clause:
    section_id: str
    parent_section: str
    section_title: str
    body: str
    action_verb: str | None
    cross_refs: list[str] = field(default_factory=list)
    is_seed: bool = False
    is_general_guideline: bool = False

    def to_chroma_metadata(self) -> dict:
        """Return a flat dict suitable for Chroma metadata.

        Chroma metadata values must be primitives (str, int, float, bool)
        or None; lists are JSON-encoded into strings.
        """
        return {
            "section_id": self.section_id,
            "parent_section": self.parent_section,
            "section_title": self.section_title,
            "action_verb": self.action_verb or "",
            "cross_refs_json": json.dumps(self.cross_refs),
            "is_seed": self.is_seed,
            "is_general_guideline": self.is_general_guideline,
        }

    def to_dict(self) -> dict:
        return asdict(self)


def _strip_markdown(text: str) -> str:
    """Remove bold/italic markers so action-verb detection works on '**must not**'."""
    return re.sub(r"\*+", "", text)


def _detect_action_verb(body: str) -> str | None:
    norm = " " + _strip_markdown(body).lower() + " "
    for verb in _VERB_ORDER:
        if f" {verb} " in norm or f" **{verb}** " in norm:
            return verb.replace(" ", "-")  # "must not" -> "must-not"
    return None


def _extract_cross_refs(body: str, *, exclude: str | None = None) -> list[str]:
    refs: set[str] = set()
    for m in CROSS_REF.finditer(body):
        refs.add(m.group(1))
    for m in SECTION_REF.finditer(body):
        refs.add(m.group(1))
    if exclude is not None:
        refs.discard(exclude)
        refs.discard(exclude.split(".")[0])
    return sorted(refs, key=lambda x: tuple(int(p) for p in x.split(".")))


def _iter_clauses_in_section(section_num: str, body: str) -> list[tuple[str, str]]:
    """Find clauses inside one section block. Returns [(clause_id, clause_body), ...]."""
    # Pattern accepts: optional H3 prefix '### ', clause id 'N.Y' or 'N.Y.Z'
    # followed by an optional period, then the body up to (but not
    # including) the next clause id or H3 heading or section heading.
    pat = re.compile(
        rf"(?:^|\n)(?:#{{1,4}}\s*)?(?P<id>{section_num}\.\d+(?:\.\d+)?)\.?\s+(?P<body>.*?)(?=\n(?:#{{1,4}}\s*)?{section_num}\.\d+(?:\.\d+)?\.?\s+|\n## Section \d+|\Z)",
        re.DOTALL,
    )
    out: list[tuple[str, str]] = []
    for m in pat.finditer(body):
        clause_id = m.group("id")
        raw_body = m.group("body").strip()
        # If the H3 form was used, the first line is a title and the
        # rest is the actual clause text. Detect by length: a line of
        # short title text followed by paragraph text. We keep both —
        # the title is informative for retrieval.
        out.append((clause_id, raw_body))
    return out


def chunk_policy(markdown: str) -> list[Clause]:
    """Parse the full policy document into a list of Clause objects.

    Sections 1-6 are tagged is_seed=True; 7+ are False.
    """
    clauses: list[Clause] = []
    matches = list(SECTION_HEADING.finditer(markdown))
    for i, m in enumerate(matches):
        section_num = m.group(1)
        section_title = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
        section_body = markdown[start:end]
        is_seed = int(section_num) <= 6
        # §6 ("General Conduct") clauses are cross-cutting: they apply to
        # every request regardless of topic. Retrieval treats them as
        # always-on so the agent prompt itself doesn't need to repeat them.
        is_general_guideline = section_num == "6"
        for clause_id, clause_body in _iter_clauses_in_section(section_num, section_body):
            clauses.append(
                Clause(
                    section_id=clause_id,
                    parent_section=section_num,
                    section_title=section_title,
                    body=clause_body,
                    action_verb=_detect_action_verb(clause_body),
                    cross_refs=_extract_cross_refs(clause_body, exclude=clause_id),
                    is_seed=is_seed,
                    is_general_guideline=is_general_guideline,
                )
            )
    return clauses


def chunk_policy_file(path: Path) -> list[Clause]:
    return chunk_policy(path.read_text())


if __name__ == "__main__":
    import sys

    repo_root = Path(__file__).resolve().parent.parent
    policy_path = repo_root / "policies" / "gaggia-it-policy.md"
    clauses = chunk_policy_file(policy_path)
    print(f"Parsed {len(clauses)} clauses from {policy_path.relative_to(repo_root)}")
    by_section: dict[str, int] = {}
    for c in clauses:
        by_section[c.parent_section] = by_section.get(c.parent_section, 0) + 1
    print("Per-section counts:")
    for sec, count in sorted(by_section.items(), key=lambda x: int(x[0])):
        marker = "(seed)" if int(sec) <= 6 else ""
        print(f"  §{sec}: {count} clauses {marker}")
    # Quick sanity sample.
    print("\nFirst 3 clauses:")
    for c in clauses[:3]:
        print(f"  §{c.section_id} [verb={c.action_verb}] refs={c.cross_refs}")
        print(f"    {c.body[:120]}{'...' if len(c.body) > 120 else ''}")
    sys.exit(0)
