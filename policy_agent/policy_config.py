"""
COMPONENT: policy_config
DESIGN-REF: D2 (filter ruleset) + D3 (tier-tool allowlist)
PURPOSE: Load and validate the two policy-as-config artifacts:
    policies/tier-tool-allowlist.yaml   — D3 (PEP source of truth)
    policies/filter-rules.yaml          — D2 (per-field disclosure rules)
  Both ship in the policy bundle, not in code, so a policy author can
  update them without touching Python.
PROBLEM-STATEMENT REQ (verbatim): >
  "Permission & Access Control Design — In production, policies evolve,
  overlap, and conflict. If you want to go further, consider how you'd
  model permissions — role-based, attribute-based, capability-based, or
  policy-as-code."
EXPECTED INPUT: yaml files at the canonical paths
EXPECTED OUTPUT: typed Python objects with validation errors raised at load
UPSTREAM: policy_agent.dispatcher (D3), policy_agent.filter (D2), tests
DOWNSTREAM: PyYAML, dataclasses
COMPONENT TESTS: tests/whitebox/test_policy_config.py
SCENARIO COVERAGE: foundation for all 21 (every scenario flows through D3+D2)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ALLOWLIST = REPO_ROOT / "policies" / "tier-tool-allowlist.yaml"
DEFAULT_FILTER_RULES = REPO_ROOT / "policies" / "filter-rules.yaml"

Tier = Literal["Red", "Blue", "Grey"]
Relationship = Literal["self", "manager_in_chain", "peer", "other"]
Disposition = Literal["allowed", "denied"]
_TIERS: tuple[Tier, ...] = ("Red", "Blue", "Grey")
_RELATIONSHIPS: tuple[Relationship, ...] = ("self", "manager_in_chain", "peer", "other")
_DISPOSITIONS: tuple[Disposition, ...] = ("allowed", "denied")


# -------- D3: tier-tool allowlist --------


@dataclass(frozen=True)
class ToolPermission:
    """One entry in a tier's allowlist.

    `arg_constraints` is a mapping the dispatcher must match against the
    tool-call arguments before authorizing. Empty means unconditional.
    """

    tool_name: str
    arg_constraints: dict[str, Any] = field(default_factory=dict)

    def matches(self, tool_name: str, args: dict[str, Any]) -> bool:
        if tool_name != self.tool_name:
            return False
        for key, expected in self.arg_constraints.items():
            if args.get(key) != expected:
                return False
        return True


@dataclass(frozen=True)
class TierToolAllowlist:
    by_tier: dict[Tier, tuple[ToolPermission, ...]]

    def is_allowed(self, tier: Tier, tool_name: str, args: dict[str, Any]) -> bool:
        for perm in self.by_tier.get(tier, ()):
            if perm.matches(tool_name, args):
                return True
        return False

    def reason_for_denial(self, tier: Tier, tool_name: str, args: dict[str, Any]) -> str:
        # Was the tool listed at all for this tier?
        listed = [p for p in self.by_tier.get(tier, ()) if p.tool_name == tool_name]
        if not listed:
            return f"tool '{tool_name}' is not in the allowlist for tier '{tier}'"
        # Listed but argument constraints didn't match.
        constraints = [p.arg_constraints for p in listed if p.arg_constraints]
        return (
            f"tool '{tool_name}' is allowed for tier '{tier}' only when "
            f"args match one of {constraints}; got {args}"
        )


def _parse_allowlist_entry(raw: Any) -> ToolPermission:
    if isinstance(raw, str):
        return ToolPermission(tool_name=raw)
    if isinstance(raw, dict):
        if "tool" not in raw:
            raise ValueError(f"allowlist entry must have 'tool' key: {raw!r}")
        return ToolPermission(
            tool_name=str(raw["tool"]),
            arg_constraints=dict(raw.get("when", {})),
        )
    raise ValueError(f"unrecognized allowlist entry: {raw!r}")


@lru_cache(maxsize=1)
def load_allowlist(path: Path | None = None) -> TierToolAllowlist:
    src = path or DEFAULT_ALLOWLIST
    raw = yaml.safe_load(src.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{src} must be a YAML mapping at the top level")
    by_tier: dict[Tier, tuple[ToolPermission, ...]] = {}
    for tier_name, entries in raw.items():
        if tier_name not in _TIERS:
            raise ValueError(f"unknown tier '{tier_name}' in {src}; allowed: {_TIERS}")
        if not isinstance(entries, list):
            raise ValueError(f"tier '{tier_name}' in {src} must map to a list")
        by_tier[tier_name] = tuple(_parse_allowlist_entry(e) for e in entries)
    # Every tier must be present (default-deny otherwise risks silent gaps).
    for tier in _TIERS:
        if tier not in by_tier:
            raise ValueError(f"{src} is missing required tier '{tier}'")
    return TierToolAllowlist(by_tier=by_tier)


# -------- D2: filter ruleset --------


@dataclass(frozen=True)
class FilterRules:
    default: Disposition
    rules: dict[str, dict[Relationship, Disposition]]

    def is_allowed(self, tag: str, relationship: Relationship) -> bool:
        rule = self.rules.get(tag)
        if rule is None:
            return self.default == "allowed"
        return rule.get(relationship, self.default) == "allowed"


@lru_cache(maxsize=1)
def load_filter_rules(path: Path | None = None) -> FilterRules:
    src = path or DEFAULT_FILTER_RULES
    raw = yaml.safe_load(src.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{src} must be a YAML mapping at the top level")
    default = raw.get("default", "denied")
    if default not in _DISPOSITIONS:
        raise ValueError(f"{src}: 'default' must be one of {_DISPOSITIONS}; got {default!r}")
    rules_raw = raw.get("rules", {})
    if not isinstance(rules_raw, dict):
        raise ValueError(f"{src}: 'rules' must be a mapping")

    parsed: dict[str, dict[Relationship, Disposition]] = {}
    for tag, body in rules_raw.items():
        if not isinstance(body, dict):
            raise ValueError(f"{src}: rule for tag '{tag}' must be a mapping")
        per_rel: dict[Relationship, Disposition] = {}
        for rel, disp in body.items():
            if rel not in _RELATIONSHIPS:
                raise ValueError(
                    f"{src}: rule for tag '{tag}' has unknown relationship '{rel}'; "
                    f"allowed: {_RELATIONSHIPS}"
                )
            if disp not in _DISPOSITIONS:
                raise ValueError(
                    f"{src}: rule for tag '{tag}', rel '{rel}' has invalid disposition "
                    f"'{disp}'; allowed: {_DISPOSITIONS}"
                )
            per_rel[rel] = disp
        # Ensure every relationship has an explicit disposition (no silent
        # fallthrough to `default` for declared tags).
        for rel in _RELATIONSHIPS:
            if rel not in per_rel:
                raise ValueError(
                    f"{src}: rule for tag '{tag}' is missing relationship '{rel}'"
                )
        parsed[tag] = per_rel
    return FilterRules(default=default, rules=parsed)


if __name__ == "__main__":
    al = load_allowlist()
    print("Tier-tool allowlist:")
    for tier in _TIERS:
        perms = al.by_tier[tier]
        for p in perms:
            extra = f" when={p.arg_constraints}" if p.arg_constraints else ""
            print(f"  {tier}: {p.tool_name}{extra}")
    fr = load_filter_rules()
    print(f"\nFilter rules (default={fr.default}, {len(fr.rules)} tags):")
    for tag, per_rel in fr.rules.items():
        compact = ", ".join(f"{r}={d}" for r, d in per_rel.items())
        print(f"  {tag}: {compact}")
