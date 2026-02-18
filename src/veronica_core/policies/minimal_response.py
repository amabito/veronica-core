"""MinimalResponsePolicy for VERONICA.

Injects response-constraint instructions into system messages to enforce
concise, structured output from LLM calls.  Operates as a message modifier,
not a shield hook -- it does not block calls, only shapes them.

Opt-in and disabled by default.  When disabled, all methods return inputs
unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from veronica_core.shield.event import SafetyEvent
from veronica_core.shield.types import Decision


_CONSTRAINT_TEMPLATE = (
    "\n\n--- RESPONSE CONSTRAINTS (enforced by VERONICA MinimalResponsePolicy) ---\n"
    "- Answer in 1 line (conclusion first).\n"
    "- Use at most {max_bullets} bullet points if elaboration needed.\n"
    "- If uncertain, state 'uncertain' in 1 line + suggest 1 next action.\n"
    "- {question_rule}\n"
    "--- END CONSTRAINTS ---"
)


@dataclass
class MinimalResponsePolicy:
    """Injects conciseness constraints into system messages.

    When enabled, appends structured constraint text to system messages.
    The original message is always preserved (constraints are appended).

    Attributes:
        enabled: Whether the policy is active. Default False.
        max_bullets: Maximum bullet points allowed. Default 5.
        allow_questions: Whether follow-up questions are permitted. Default False.
        max_questions: Maximum questions if allowed. Default 1.
    """

    enabled: bool = False
    max_bullets: int = 5
    allow_questions: bool = False
    max_questions: int = 1

    def _build_constraints(self) -> str:
        """Build the constraint text block."""
        if self.allow_questions:
            question_rule = f"At most {self.max_questions} question if essential."
        else:
            question_rule = "No follow-up questions."
        return _CONSTRAINT_TEMPLATE.format(
            max_bullets=self.max_bullets,
            question_rule=question_rule,
        )

    def inject(self, system_message: str) -> str:
        """Append constraint text to a system message.

        Args:
            system_message: Original system message content.

        Returns:
            Original message with constraints appended (if enabled),
            or the original message unchanged (if disabled).
        """
        if not self.enabled:
            return system_message
        return system_message + self._build_constraints()

    def wrap_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """Inject policy into a request dict.

        Looks for a 'system' key and applies inject().
        Preserves the original system message in '_original_system'
        for audit purposes.

        Args:
            request: Request dict (must contain 'system' key to be modified).

        Returns:
            New dict with modified 'system' and preserved '_original_system'.
            If disabled or no 'system' key, returns the input unchanged.
        """
        if not self.enabled:
            return request
        if "system" not in request:
            return request
        result = dict(request)
        result["_original_system"] = request["system"]
        result["system"] = self.inject(request["system"])
        return result

    def create_event(self, request_id: str | None = None) -> SafetyEvent:
        """Create a SafetyEvent recording that this policy was applied.

        Always returns an event -- caller decides whether to record it
        (typically only when enabled).
        """
        return SafetyEvent(
            event_type="POLICY_APPLIED",
            decision=Decision.ALLOW,
            reason="minimal_response_policy applied",
            hook="MinimalResponsePolicy",
            request_id=request_id,
        )
