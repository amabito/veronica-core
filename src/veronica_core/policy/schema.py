"""Declarative policy schema for VERONICA Execution Shield.

Uses stdlib dataclasses only (zero external dependencies).
Policies are expressed as YAML/JSON files and loaded via PolicyLoader.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, get_args


OnExceed = Literal["halt", "degrade", "queue", "warn", "custom"]

_VALID_ON_EXCEED: frozenset[str] = frozenset(get_args(OnExceed))

# Pairs of on_exceed values that are contradictory on the same rule.
_CONTRADICTORY_PAIRS = frozenset(
    {
        frozenset({"halt", "degrade"}),
    }
)


class PolicyValidationError(Exception):
    """Raised when a policy schema fails validation.

    Attributes:
        errors: List of human-readable error messages.
        field:  Optional field name that triggered the error.
    """

    def __init__(self, errors: list[str], field_name: str | None = None) -> None:
        self.errors = errors
        self.field_name = field_name
        super().__init__("; ".join(errors))


@dataclass
class RuleSchema:
    """Single rule within a policy.

    Attributes:
        type:      Rule type identifier (e.g. "token_budget", "circuit_breaker").
        params:    Rule-specific parameters forwarded to the factory.
        on_exceed: Action when the rule is exceeded.
                   One of: halt | degrade | queue | warn | custom.
    """

    type: str
    params: dict[str, Any] = field(default_factory=dict)
    on_exceed: OnExceed = "halt"

    def __post_init__(self) -> None:
        errors: list[str] = []
        if not self.type or not isinstance(self.type, str):
            errors.append("RuleSchema.type must be a non-empty string")
        if not isinstance(self.on_exceed, str) or self.on_exceed not in _VALID_ON_EXCEED:
            errors.append(
                f"RuleSchema.on_exceed={self.on_exceed!r} is invalid; "
                f"must be one of {sorted(_VALID_ON_EXCEED)}"
            )
        if self.params is None:
            self.params = {}
        if errors:
            raise PolicyValidationError(errors)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuleSchema":
        """Construct from a plain dict, ignoring unknown keys."""
        return cls(
            type=data.get("type", ""),
            params=dict(data.get("params") or {}),
            on_exceed=data.get("on_exceed", "halt"),  # type: ignore[arg-type]
        )


@dataclass
class PolicySchema:
    """Top-level policy schema.

    Attributes:
        version: Schema version string (e.g. "1.0").
        name:    Human-readable policy name.
        rules:   Ordered list of rules to apply.
    """

    version: str
    name: str
    rules: list[RuleSchema] = field(default_factory=list)

    def __post_init__(self) -> None:
        errors: list[str] = []
        if not self.version or not isinstance(self.version, str):
            errors.append("PolicySchema.version must be a non-empty string")
        if not self.name or not isinstance(self.name, str):
            errors.append("PolicySchema.name must be a non-empty string")
        if self.rules is None:
            self.rules = []
        else:
            # Detect contradictory rules: same type, conflicting on_exceed values.
            _type_exceed: dict[str, set[str]] = {}
            for rule in self.rules:
                _type_exceed.setdefault(rule.type, set()).add(rule.on_exceed)
            for rule_type, exceed_set in _type_exceed.items():
                for pair in _CONTRADICTORY_PAIRS:
                    if pair.issubset(exceed_set):
                        errors.append(
                            f"Contradictory on_exceed values {sorted(pair)} "
                            f"for rule type {rule_type!r}"
                        )
        if errors:
            raise PolicyValidationError(errors)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PolicySchema":
        """Construct from a plain dict, ignoring unknown keys."""
        raw_rules = data.get("rules") or []
        rules: list[RuleSchema] = []
        for i, r in enumerate(raw_rules):
            if r is None or not isinstance(r, dict):
                raise PolicyValidationError(
                    [f"rules[{i}] must be a dict, got {type(r).__name__}"],
                    field_name="rules",
                )
            rules.append(RuleSchema.from_dict(r))
        # Use "" for None so __post_init__ validation catches null version/name.
        raw_version = data.get("version")
        raw_name = data.get("name")
        return cls(
            version="" if raw_version is None else str(raw_version),
            name="" if raw_name is None else str(raw_name),
            rules=rules,
        )
