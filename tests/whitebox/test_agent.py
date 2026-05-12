"""
TESTS-FOR: policy_agent.agent — system-prompt purity

PURPOSE: The problem statement requires policy to be retrievable, not
  embedded in the prompt. This guard test pins that contract: the
  ``SYSTEM_PROMPT_TEMPLATE`` (and any tier blocks composed into it) must
  not contain section IDs from the policy itself — those are supplied
  through retrieved chunks at runtime.

Scope: the prompt template, BLUE_TIER_BLOCK, GREY_TIER_BLOCK, and
INJECTION_FLAG_BLOCK. The DENY_EXEMPLAR / ALLOW_EXEMPLAR are
intentionally excluded — they're response-format demonstrations, not
policy enforcement, and the user explicitly scoped them out.
"""
from __future__ import annotations

import re

import pytest

from policy_agent.agent import (
    BLUE_TIER_BLOCK,
    GREY_TIER_BLOCK,
    INJECTION_FLAG_BLOCK,
    SYSTEM_PROMPT_TEMPLATE,
)

# Matches "§N", "§N.M", "§N.M.K" — the section-id citation style used
# throughout the policy. Hits inside this template indicate that policy
# has been re-hardcoded into the prompt.
_SECTION_REF = re.compile(r"§\s*\d+(?:\.\d+)*")


@pytest.mark.parametrize(
    "name,block",
    [
        ("SYSTEM_PROMPT_TEMPLATE", SYSTEM_PROMPT_TEMPLATE),
        ("BLUE_TIER_BLOCK", BLUE_TIER_BLOCK),
        ("GREY_TIER_BLOCK", GREY_TIER_BLOCK),
        ("INJECTION_FLAG_BLOCK", INJECTION_FLAG_BLOCK),
    ],
)
def test_no_policy_section_ids_in_prompt_block(name: str, block: str):
    hits = _SECTION_REF.findall(block)
    assert hits == [], (
        f"{name} contains policy section references {hits}. Policy must "
        "be retrievable, not hardcoded into the prompt — move the rule "
        "into policies/ and rely on retrieval."
    )


@pytest.mark.parametrize(
    "phrase",
    [
        "claimed authority",  # the §6.3 paraphrase
        "do not speculate",  # the §6.2 paraphrase
        "performance reviews",  # the §4.2 example fragment
    ],
)
def test_no_policy_rule_paraphrases_in_system_prompt(phrase: str):
    assert phrase.lower() not in SYSTEM_PROMPT_TEMPLATE.lower(), (
        f"SYSTEM_PROMPT_TEMPLATE contains the phrase {phrase!r}, which "
        "paraphrases a specific policy clause. Behaviour rules in the "
        "prompt should be generic; policy text comes from retrieval."
    )
