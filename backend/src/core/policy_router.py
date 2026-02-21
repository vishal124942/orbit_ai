"""
Policy Router — Hard Rules Engine
Takes the analysis JSON from LLM-1 and makes a deterministic routing decision.

Routes:
  AUTO_REPLY       → Respond automatically (default for everything)
  DRAFT_FOR_HUMAN  → Generate a reply but hold for human approval
  HANDOFF          → Alert operator, do NOT auto-reply

NOTE: Toxicity / vulgarity / banter is NEVER a routing signal here.
The only things that trigger non-auto routes are practical, real-world
situations where a human needs to be in the loop (money, emergencies).
"""

from typing import Dict, Tuple

# Route constants
ROUTE_AUTO_REPLY       = "AUTO_REPLY"
ROUTE_DRAFT_FOR_HUMAN  = "DRAFT_FOR_HUMAN"
ROUTE_HANDOFF          = "HANDOFF"


class PolicyRouter:
    """
    Deterministic rule engine. No LLM — pure logic.
    Order matters: rules evaluated top to bottom, first match wins.
    """

    def __init__(self, config: Dict):
        policy = config.get("policy", {})

        # Intents that always require a human (real-world consequences)
        self.handoff_intents = set(
            policy.get("handoff_intents", ["money", "emergency"])
        )

        # Intents that generate a draft but don't auto-send
        self.draft_intents = set(
            policy.get("draft_intents", [])
        )

    def route(self, analysis: Dict) -> Tuple[str, str]:
        """
        Evaluate analysis JSON → (route, reason).
        Vibe, toxicity, gaali — none of these affect routing.
        Only real-world-consequence intents do.
        """
        intent = analysis.get("intent", "casual")

        # Rule 1: Money / Emergency → HANDOFF (human must handle)
        if intent in self.handoff_intents:
            return ROUTE_HANDOFF, f"Real-world sensitive intent: {intent}"

        # Rule 2: Draft intents (configurable, empty by default)
        if intent in self.draft_intents:
            return ROUTE_DRAFT_FOR_HUMAN, f"Draft for intent: {intent}"

        # Default: AUTO_REPLY — banter, gaali, roast, conflict, everything else
        return ROUTE_AUTO_REPLY, f"Auto-reply (intent={intent})"

    def describe(self, route: str, reason: str) -> str:
        icons = {
            ROUTE_AUTO_REPLY:      "🤖 AUTO_REPLY",
            ROUTE_DRAFT_FOR_HUMAN: "✏️  DRAFT_FOR_HUMAN",
            ROUTE_HANDOFF:         "🚨 HANDOFF",
        }
        return f"{icons.get(route, route)} — {reason}"