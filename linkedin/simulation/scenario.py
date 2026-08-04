# linkedin/simulation/scenario.py
"""Scenario definition and loading for conversation simulation.

A scenario describes a fictional prospect and their hidden context.
Visible fields are shared with the Conversation Agent via the prompt.
Hidden fields are only visible to the Prospect Simulator.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path


@dataclass
class Scenario:
    """A fictional prospect scenario for conversation simulation.

    Visible fields (shared with the Conversation Agent):
        name: Prospect's display name.
        role: Job title.
        company: Company name.
        industry: Industry sector.
        country: Location.
        personality: Personality traits (e.g. "cautious, analytical").
        communication_style: How they communicate (e.g. "brief, direct").
        current_priorities: What they care about right now.

    Hidden fields (visible ONLY to the Prospect Simulator):
        hidden_business_context: Real situation at their company.
        hidden_attitude_to_ai: How they feel about AI/automation.
        hidden_buying_interest: Whether they'd actually buy (0-10).
        hidden_decision_authority: Their actual purchase power.
        hidden_behavior: How they should behave (e.g. "friendly but evasive").
    """

    # Visible fields
    name: str = ""
    role: str = ""
    company: str = ""
    industry: str = ""
    country: str = ""
    personality: str = ""
    communication_style: str = ""
    current_priorities: str = ""

    # Hidden fields (prospect simulator only)
    hidden_business_context: str = ""
    hidden_attitude_to_ai: str = ""
    hidden_buying_interest: int = 5
    hidden_decision_authority: str = ""
    hidden_behavior: str = ""

    @property
    def visible_context(self) -> dict:
        """Return only the visible fields (safe to share with the agent)."""
        return {
            "name": self.name,
            "role": self.role,
            "company": self.company,
            "industry": self.industry,
            "country": self.country,
            "personality": self.personality,
            "communication_style": self.communication_style,
            "current_priorities": self.current_priorities,
        }

    @property
    def hidden_context(self) -> dict:
        """Return only the hidden fields (prospect simulator only)."""
        return {
            "hidden_business_context": self.hidden_business_context,
            "hidden_attitude_to_ai": self.hidden_attitude_to_ai,
            "hidden_buying_interest": self.hidden_buying_interest,
            "hidden_decision_authority": self.hidden_decision_authority,
            "hidden_behavior": self.hidden_behavior,
        }

    def to_dict(self) -> dict:
        return asdict(self)


def load_scenario(path: str | Path) -> Scenario:
    """Load a scenario from a JSON file."""
    path = Path(path)
    with open(path) as f:
        data = json.load(f)
    return Scenario(**data)


def save_scenario(scenario: Scenario, path: str | Path) -> None:
    """Save a scenario to a JSON file."""
    path = Path(path)
    with open(path, "w") as f:
        json.dump(scenario.to_dict(), f, indent=2)


def default_scenario() -> Scenario:
    """Return the default sample scenario."""
    return Scenario(
        name="Alex Chen",
        role="VP of Operations",
        company="MediFlow Health",
        industry="Healthcare",
        country="United States",
        personality="Cautious, analytical, data-driven",
        communication_style="Polite but brief. Prefers bullet points.",
        current_priorities="Reducing operational costs, improving patient intake efficiency",
        hidden_business_context="Company is under pressure to cut 15% costs this year. "
        "Currently evaluating several automation vendors but decision is politically sensitive.",
        hidden_attitude_to_ai="Skeptical but curious. Has seen too many overhyped demos.",
        hidden_buying_interest=6,
        hidden_decision_authority="Can recommend but needs board approval for >$50k",
        hidden_behavior="Friendly but evasive on specifics. Will not commit early.",
    )
