# linkedin/simulation/transcript.py
"""Transcript recording for simulation conversations.

Appends each turn to a human-readable text file showing prospect replies,
agent decisions, and internal state at each step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class TranscriptTurn:
    """A single turn in a simulated conversation."""

    turn_number: int
    prospect_reply: str
    agent_action: str
    agent_objective_category: str
    agent_message: str | None
    agent_outcome: str | None
    recipient_state: dict | None = None
    conversation_state: dict | None = None
    relationship_state: dict | None = None
    belief_state: dict | None = None
    conversation_summary: str | None = None
    action_reason: str | None = None


@dataclass
class Transcript:
    """Records a full simulation conversation."""

    scenario_name: str
    turns: list[TranscriptTurn] = field(default_factory=list)

    def add_turn(self, turn: TranscriptTurn) -> None:
        self.turns.append(turn)

    @property
    def total_turns(self) -> int:
        return len(self.turns)

    @property
    def ended_early(self) -> bool:
        """True if the conversation ended before reaching the turn limit."""
        if not self.turns:
            return False
        last = self.turns[-1]
        return last.agent_action == "mark_completed"

    def write(self, path: str | Path) -> None:
        """Write the transcript to a text file."""
        path = Path(path)
        lines = [
            "=" * 50,
            "Scenario",
            "=" * 50,
            "",
            self.scenario_name,
            "",
        ]

        for turn in self.turns:
            lines.extend(self._format_turn(turn))
            lines.append("")

        path.write_text("\n".join(lines))

    @staticmethod
    def _format_turn(turn: TranscriptTurn) -> list[str]:
        """Format a single turn as readable text lines.

        Conversation Agent always messages first (the outreach),
        then the Prospect replies. The transcript reflects this order.
        """
        lines = [
            "=" * 50,
            f"Turn {turn.turn_number}",
            "=" * 50,
            "",
            "Conversation Agent",
            "",
            f"  Action: {turn.agent_action}",
            f"  Objective: {turn.agent_objective_category}",
        ]

        if turn.agent_message:
            lines.append(f"  Message: {turn.agent_message}")
        if turn.agent_outcome:
            lines.append(f"  Outcome: {turn.agent_outcome}")
        if turn.conversation_summary:
            lines.append(f"  Conversation Summary: {turn.conversation_summary}")
        if turn.action_reason:
            lines.append(f"  Action Reason: {turn.action_reason}")

        if turn.recipient_state:
            lines.append("")
            lines.append("  Recipient State:")
            for k, v in turn.recipient_state.items():
                lines.append(f"    {k}: {v}")
        if turn.conversation_state:
            lines.append("")
            lines.append("  Conversation State:")
            for k, v in turn.conversation_state.items():
                lines.append(f"    {k}: {v}")
        if turn.relationship_state:
            lines.append("")
            lines.append("  Relationship State:")
            for k, v in turn.relationship_state.items():
                lines.append(f"    {k}: {v}")
        if turn.belief_state:
            lines.append("")
            lines.append("  Belief State:")
            for k, v in turn.belief_state.items():
                lines.append(f"    {k}: {v}")

        lines.extend([
            "",
            "Prospect",
            "",
            turn.prospect_reply if turn.prospect_reply else "(no reply — agent ended conversation)",
        ])

        return lines
