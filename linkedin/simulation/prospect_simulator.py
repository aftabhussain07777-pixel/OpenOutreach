# linkedin/simulation/prospect_simulator.py
"""Prospect Simulator — an LLM agent that role-plays a realistic prospect.

The simulator models a busy professional who did not opt into this conversation.
It does NOT help the sales agent. Replies are short, natural, and phone-like.
Buying signals are rare. The goal is realism, not conversation quality.
"""

from __future__ import annotations

import logging
import random

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from linkedin.llm import get_conversation_llm_model, run_agent_sync

from linkedin.simulation.scenario import Scenario

logger = logging.getLogger(__name__)

# ~60% of replies should be 1 sentence, ~30% 2 sentences, ~10% 3-5 sentences.
# This is enforced via the prompt + the sampler below.
_SHORT_REPLY_WEIGHT = 0.6
_MEDIUM_REPLY_WEIGHT = 0.3


SYSTEM_PROMPT_TEMPLATE = """\
You are simulating {name}, a real professional who happens to receive a LinkedIn message.

--- CHARACTER ---
Name: {name}
Role: {role}
Company: {company}
Industry: {industry}
Country: {country}
Personality: {personality}
Communication Style: {communication_style}
Current Priorities: {current_priorities}

--- HIDDEN CONTEXT (only you know) ---
Business Context: {hidden_business_context}
Attitude to AI: {hidden_attitude_to_ai}
Buying Interest (0-10): {hidden_buying_interest}
Decision Authority: {hidden_decision_authority}
Behavior: {hidden_behavior}

--- THE GOLDEN RULE ---
{name} did NOT ask for this conversation.
{name} did NOT opt in.
{name} is busy.
This LinkedIn message is a low-priority interruption among dozens of notifications.

Your ONLY job is to simulate how {name} would naturally react.
Do NOT help the seller.
Do NOT create opportunities.
Do NOT volunteer business problems.
Do NOT make the seller's job easier.

--- BEHAVIOR RULES ---
- Answer naturally, as if replying between meetings or from a phone.
- If asked a question, you may answer it, OR ignore it, OR answer only part of it.
- Do NOT answer questions you already answered. Move on or change the topic.
- Do NOT repeat information. Once you said something, it was said.
- Do NOT expand on answers unless you are genuinely interested.
- You may ask a question back if it feels natural — not to "help the conversation" but because you're curious.
- You may misunderstand or misinterpret what was said — real people do this.
- You become busier / less interested as the conversation goes on (more turns = less patience).
- You may politely end the conversation at any time.
- You may stop replying (continue_conversation = false) without explanation.

--- FORBIDDEN BEHAVIORS ---
Never do any of the following:
- Write bullet lists or numbered lists.
- Write proposal requirements or evaluation criteria.
- Say "I appreciate you reaching out" or "Thanks for the thoughtful message" — real people don't say this to strangers on LinkedIn.
- Say "That's a great question" — real people don't narrate their own reactions.
- Explain your situation unprompted.
- Summarize your needs.
- Help the seller qualify you.
- Behave like you're in a procurement process.
- Repeat or rephrase what you already said.

--- MESSAGE STYLE ---
Write like a real person replying from their phone:
- Use natural, casual language. Contractions are normal ("I'm", "that's", "don't").
- Most replies are ONE sentence long.
- Sometimes TWO sentences.
- Almost never write three or more sentences.
- Do NOT use formal or sales-like language.
- Do NOT explain your reasoning.
- Typos and sentence fragments are fine. Real people don't proofread LinkedIn DMs.

--- BUYING SIGNALS ---
Buying signals are RARE events.
Do NOT generate buying signals just because the seller asked a good question or seemed helpful.

A buying signal (showing genuine interest in a solution) should ONLY emerge if ALL of:
1. The scenario's buying interest is above 5 (currently {hidden_buying_interest}/10).
2. The seller has built enough context through natural conversation.
3. It feels inevitable given the scenario and conversation history.

Otherwise, stay neutral, skeptical, or politely non-committal.
"Yes, that's interesting" is NOT a buying signal — it's politeness.

--- CONSISTENCY ---
Your reply must be consistent with:
- Your personality: {personality}
- Your communication style: {communication_style}
- Your current priorities: {current_priorities}
- Your hidden context: {hidden_business_context}
- What you have ALREADY SAID in this conversation (do not contradict yourself)

--- FATIGUE ---
This is turn {turn_number} of this conversation.
With each turn, {name} has less patience and attention to give.
Longer conversations should trend toward shorter replies and higher chance of ending.

--- CONVERSATION SO FAR ---
{conversation_history}

--- LATEST MESSAGE FROM SELLER ---
{seller_message}

--- RESPOND AS {name} ---
Reply naturally. Keep it brief. Write one sentence."""


class ProspectReply(BaseModel):
    """Structured output from the prospect simulator."""

    reply: str = Field(
        description="The prospect's reply to the last message. "
        "Natural, brief, phone-like language. One sentence preferred.",
    )
    continue_conversation: bool = Field(
        description="Whether the prospect wants to continue the conversation. "
        "False when the prospect stops replying, loses interest, "
        "gets busy, or the conversation has naturally ended.",
    )


class ProspectSimulator:
    """Simulates a prospect in a LinkedIn conversation.

    Uses an LLM agent to generate replies that stay in character
    according to the scenario definition. Tracks conversation state
    internally to enforce consistency, avoid repetition, and model
    natural fatigue over multiple turns.
    """

    def __init__(self, scenario: Scenario):
        self.scenario = scenario
        self._turn_count = 0
        self._topics_mentioned: set[str] = set()
        self._agent = Agent(
            get_conversation_llm_model(),
            output_type=ProspectReply,
            model_settings={"temperature": 0.9, "timeout": 60},
        )

    def _extract_topics(self, text: str) -> set[str]:
        """Extract rough topic keywords from a message for dedup tracking."""
        # Simple extraction: lowercased words > 4 chars that aren't stop-words
        stop_words = {
            "about", "there", "their", "would", "could", "should", "really",
            "things", "thing", "being", "doing", "going", "something", "that's",
            "because", "people", "think", "thanks", "thank", "right", "quite",
            "maybe", "always", "never", "already", "actually", "basically",
            "honestly", "anyway", "though", "either", "still", "well", "just",
            "very", "also", "more", "some", "much", "here", "what", "which",
            "were", "been", "have", "from", "they", "this", "that", "with",
        }
        words = text.lower().split()
        return {w for w in words if len(w) > 4 and w not in stop_words}

    def generate_reply(
        self,
        conversation_history: list[dict],
        seller_message: str,
    ) -> ProspectReply:
        """Generate the next prospect reply given the conversation context.

        Args:
            conversation_history: List of dicts with ``role`` ("prospect"/"seller")
                and ``content`` keys, in chronological order.
            seller_message: The most recent seller message to respond to.

        Returns:
            A ``ProspectReply`` with the reply text and continue flag.
        """
        self._turn_count += 1

        # Format conversation history as a transcript
        history_lines = []
        for msg in conversation_history:
            speaker = "Seller" if msg["role"] == "seller" else "Prospect"
            history_lines.append(f"{speaker}: {msg['content']}")
        history_str = "\n".join(history_lines) if history_lines else "(conversation just started)"

        # Track topics from prospect's past replies to prevent repetition
        for msg in conversation_history:
            if msg["role"] == "prospect":
                self._topics_mentioned |= self._extract_topics(msg["content"])

        prompt = SYSTEM_PROMPT_TEMPLATE.format(
            **self.scenario.to_dict(),
            turn_number=self._turn_count,
            conversation_history=history_str,
            seller_message=seller_message,
        )

        result = run_agent_sync(self._agent.run(prompt)).output
        if result is None:
            raise RuntimeError("Prospect simulator returned unparseable output")

        # Post-process: if reply is long, sample a shorter version probabilistically
        result.reply = self._sample_reply_length(result.reply)

        # Track new topics from this reply
        self._topics_mentioned |= self._extract_topics(result.reply)

        return result

    def _sample_reply_length(self, reply: str) -> str:
        """Probabilistically shorten replies to enforce realistic length distribution.

        The LLM prompt asks for short replies, but models sometimes ignore this.
        This post-process step provides a safety net.
        """
        sentences = self._split_sentences(reply)
        num_sentences = len(sentences)

        if num_sentences <= 1:
            return reply

        # Decide target length based on weighted distribution
        roll = random.random()
        if roll < _SHORT_REPLY_WEIGHT:
            target = 1
        elif roll < _SHORT_REPLY_WEIGHT + _MEDIUM_REPLY_WEIGHT:
            target = 2
        else:
            target = min(5, num_sentences)  # cap at 5

        if num_sentences <= target:
            return reply

        # Keep the first `target` sentences (most natural for replies)
        return " ".join(sentences[:target])

    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        """Split text into sentences by common sentence boundaries."""
        import re
        # Split on sentence-ending punctuation followed by space or end of string
        parts = re.split(r"(?<=[.!?])\s+", text.strip())
        return [p.strip() for p in parts if p.strip()]
