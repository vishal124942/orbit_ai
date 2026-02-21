"""
Memory Manager — Three-Tier Architecture
==========================================

Tier 1 — SHORT-TERM (in-RAM):
  Last N conversation turns for the active window.
  Fastest access, wiped on restart.
  ~15-30 messages. Used as direct LLM context.

Tier 2 — LONG-TERM (SQLite):
  Durable facts about a person: name, job, preferences, tone, gaali_tolerance.
  Written by the Reflection step, loaded at session start.
  Persists forever. Key-value per JID.

Tier 3 — EPISODIC (SQLite):
  Specific memorable moments: "got drunk and was hilarious on Feb 3",
  "his girlfriend's name is Shreya", "super sad about his dog dying",
  "had a fight with his manager Ravi".
  Each episode has: timestamp, importance score, tags, and summary text.
  Retrieved by relevance to current conversation using keyword matching.
  Injected into LLM context when relevant episodes are found.
"""

import json
import re
import os
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
from openai import OpenAI


# ─────────────────────────────────────────────────────────────────────────────
# Episode Schema
# ─────────────────────────────────────────────────────────────────────────────
class Episode:
    def __init__(
        self,
        episode_id: int,
        remote_jid: str,
        summary: str,
        importance: float,           # 0.0 – 1.0
        emotion: str,                # dominant emotion
        tags: List[str],             # keyword tags for retrieval
        created_at: str,
        message_ids: List[str],      # message IDs this episode covers
    ):
        self.episode_id = episode_id
        self.remote_jid = remote_jid
        self.summary = summary
        self.importance = importance
        self.emotion = emotion
        self.tags = tags
        self.created_at = created_at
        self.message_ids = message_ids

    def to_context_string(self) -> str:
        date = self.created_at[:10]  # YYYY-MM-DD
        return f"[Memory {date}] {self.summary} (emotion: {self.emotion})"


# ─────────────────────────────────────────────────────────────────────────────
# Prompts
# ─────────────────────────────────────────────────────────────────────────────

EPISODE_EXTRACTION_PROMPT = """
You are extracting memorable episodes from a WhatsApp conversation for long-term memory storage.

An episode is worth storing if it contains:
- Personal facts (relationships, events, life updates)
- Strong emotions (anger, sadness, joy, embarrassment)  
- Important decisions or events
- Funny/memorable moments
- Conflicts, achievements, problems

For each episode found, return a JSON array:
[
  {
    "summary": "<one crisp sentence capturing the core memory>",
    "importance": <0.0 to 1.0>,
    "emotion": "<primary emotion: happy/sad/angry/excited/nostalgic/drunk/funny/anxious/etc>",
    "tags": ["<keyword1>", "<keyword2>", ...]
  }
]

If nothing episodic happened (just casual chat, nothing memorable), return [].
Be selective — not every message needs an episode. Quality over quantity.
"""

LONG_TERM_EXTRACTION_PROMPT = """
Extract durable facts about this person from the conversation.
Return ONLY a JSON object of new or updated facts to remember long-term.

Categories:
- name: their first name
- nickname: what they want to be called  
- relationship: how they relate to you (best friend, cousin, colleague, etc.)
- job: their profession/company
- city: where they live
- age: their age
- tone: how they communicate (casual/heavy-gaali/formal/roaster/etc)
- gaali_tolerance: none/low/medium/high (based on language they actually use)
- interests: hobbies, passions
- family: names and relations
- relationships: girlfriend, boyfriend, spouse name
- pets: pet names
- preferences: food, music, anything they've mentioned liking/disliking

Return {} if nothing new to add.
"""

MEMORY_RELEVANCE_PROMPT = """
Given the current conversation message(s) and a list of past memories,
return ONLY the IDs of memories that are relevant to the current context.
Return a JSON array of integers: [1, 5, 12] or [] if none are relevant.

Current message:
{current_message}

Past memories:
{memories_json}
"""


# ─────────────────────────────────────────────────────────────────────────────
# Memory Manager
# ─────────────────────────────────────────────────────────────────────────────

class MemoryManager:
    """
    Three-tier memory for each JID:
    - Short-term: in-RAM sliding window (managed externally via session history)
    - Long-term:  persistent key-value facts (SQLite)
    - Episodic:   timestamped memorable moments (SQLite)
    """

    SHORT_TERM_WINDOW = 20  # messages in active context
    EPISODE_INJECT_LIMIT = 5  # max episodes to inject per turn
    REFLECTION_EVERY_N = 12  # run reflection every N messages

    def __init__(self, db, openai_client: OpenAI, config: Dict):
        self.db = db
        self.openai_client = openai_client
        self.config = config
        
        # Sarvam setup
        self.client = openai_client
        self.model = config.get("openai", {}).get("model", "gpt-4o")
        
        sarvam_cfg = config.get("sarvam", {})
        if sarvam_cfg.get("enabled", False) or os.getenv("SARVAM_API_KEY"):
            sarvam_key = os.getenv("SARVAM_API_KEY")
            if sarvam_key:
                try:
                    self.client = OpenAI(
                        api_key=sarvam_key,
                        base_url=sarvam_cfg.get("api_base", "https://api.sarvam.ai/v1"),
                    )
                    self.model = "sarvam-m"
                except Exception:
                    pass
        
        # In-RAM short-term per JID
        self._short_term: Dict[str, List[Dict]] = {}

    # ──────────────────────────────────────────────────────────────────────────
    # Short-Term Memory
    # ──────────────────────────────────────────────────────────────────────────

    def add_to_short_term(self, remote_jid: str, role: str, content: str):
        """Add a message to the short-term sliding window."""
        if remote_jid not in self._short_term:
            self._short_term[remote_jid] = []
        self._short_term[remote_jid].append({"role": role, "content": content})
        # Trim to window
        if len(self._short_term[remote_jid]) > self.SHORT_TERM_WINDOW:
            self._short_term[remote_jid] = self._short_term[remote_jid][-self.SHORT_TERM_WINDOW:]

    def get_short_term(self, remote_jid: str) -> List[Dict]:
        return self._short_term.get(remote_jid, [])

    def get_short_term_count(self, remote_jid: str) -> int:
        return len(self._short_term.get(remote_jid, []))

    # ──────────────────────────────────────────────────────────────────────────
    # Long-Term Memory
    # ──────────────────────────────────────────────────────────────────────────

    def get_long_term(self, remote_jid: str) -> Dict[str, Any]:
        """Load all long-term facts for a JID."""
        session = self.db.get_session(remote_jid)
        if session:
            try:
                return json.loads(session["intelligence"] or "{}")
            except Exception:
                pass
        return {}

    def update_long_term(self, remote_jid: str, facts: Dict[str, Any]):
        """Merge new facts into long-term memory."""
        existing = self.get_long_term(remote_jid)
        existing.update(facts)
        self.db.update_session(remote_jid, intelligence=json.dumps(existing))
        return existing

    def set_long_term_key(self, remote_jid: str, key: str, value: str):
        """Set a single long-term fact."""
        facts = self.get_long_term(remote_jid)
        facts[key] = value
        self.db.update_session(remote_jid, intelligence=json.dumps(facts))

    def format_long_term_context(self, remote_jid: str) -> str:
        """Format long-term facts as a readable context block."""
        facts = self.get_long_term(remote_jid)
        if not facts:
            return ""
        lines = [f"  {k}: {v}" for k, v in facts.items()]
        return "[LONG-TERM MEMORY — Who this person is]:\n" + "\n".join(lines)

    # ──────────────────────────────────────────────────────────────────────────
    # Episodic Memory
    # ──────────────────────────────────────────────────────────────────────────

    async def extract_and_store_episodes(self, remote_jid: str, recent_messages: List[Dict]):
        """
        Run LLM reflection on recent messages to extract memorable episodes.
        Called periodically (every REFLECTION_EVERY_N messages).
        """
        if not recent_messages:
            return

        conversation_text = "\n".join([
            f"{'Orbit' if m.get('from_me') else m.get('push_name', 'User')}: {m.get('text', '')}"
            for m in recent_messages
            if m.get("text")
        ])

        if not conversation_text.strip():
            return

        # 1. Extract episodes
        try:
            kwargs = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": EPISODE_EXTRACTION_PROMPT},
                    {"role": "user", "content": conversation_text},
                ],
                "max_tokens": 600,
                "temperature": 0.2,
            }
            if self.model != "sarvam-m":
                kwargs["response_format"] = {"type": "json_object"}

            response = self.client.chat.completions.create(**kwargs)
            raw = response.choices[0].message.content
            # Handle both array and object responses
            parsed = json.loads(raw)
            episodes = parsed if isinstance(parsed, list) else parsed.get("episodes", [])
        except Exception as e:
            print(f"[MemoryManager] Episode extraction failed: {e}")
            episodes = []

        for ep in episodes:
            if not ep.get("summary"):
                continue
            self.db.store_episode(
                remote_jid=remote_jid,
                summary=ep.get("summary", ""),
                importance=float(ep.get("importance", 0.5)),
                emotion=ep.get("emotion", "neutral"),
                tags=ep.get("tags", []),
            )
            print(f"[MemoryManager] 💾 Episode stored: {ep.get('summary', '')[:60]}")

        # 2. Extract long-term facts in the same pass
        try:
            kwargs = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": LONG_TERM_EXTRACTION_PROMPT},
                    {"role": "user", "content": conversation_text},
                ],
                "max_tokens": 300,
                "temperature": 0.1,
            }
            if self.model != "sarvam-m":
                kwargs["response_format"] = {"type": "json_object"}

            response = self.client.chat.completions.create(**kwargs)
            facts = json.loads(response.choices[0].message.content)
            if facts:
                merged = self.update_long_term(remote_jid, facts)
                print(f"[MemoryManager] 🧠 Long-term updated: {list(facts.keys())}")
        except Exception as e:
            print(f"[MemoryManager] Long-term extraction failed: {e}")

    def get_relevant_episodes(self, remote_jid: str, current_text: str, limit: int = None) -> List[Dict]:
        """
        Fast keyword-based retrieval of relevant episodes.
        Scores episodes by: keyword overlap + recency + importance.
        """
        limit = limit or self.EPISODE_INJECT_LIMIT
        all_episodes = self.db.get_episodes(remote_jid, limit=50)
        if not all_episodes:
            return []

        # Tokenize current text for matching
        current_words = set(re.findall(r"\w+", current_text.lower()))

        scored = []
        for ep in all_episodes:
            tags = json.loads(ep["tags"]) if isinstance(ep["tags"], str) else ep["tags"]
            tag_words = set(w.lower() for t in tags for w in t.split())

            # Keyword overlap score
            overlap = len(current_words & tag_words)
            # Summary word overlap
            summary_words = set(re.findall(r"\w+", ep["summary"].lower()))
            summary_overlap = len(current_words & summary_words)

            score = (overlap * 2 + summary_overlap) * float(ep["importance"])

            if score > 0:
                scored.append((score, ep))

        # Sort by score desc, then by recency
        scored.sort(key=lambda x: x[0], reverse=True)
        return [ep for _, ep in scored[:limit]]

    def format_episodic_context(self, remote_jid: str, current_text: str) -> str:
        """Format relevant episodes as context block for the LLM."""
        episodes = self.get_relevant_episodes(remote_jid, current_text)
        if not episodes:
            return ""

        lines = []
        for ep in episodes:
            tags = json.loads(ep["tags"]) if isinstance(ep["tags"], str) else ep["tags"]
            date = ep["created_at"][:10]
            lines.append(f"  [{date}] {ep['summary']} (feel: {ep['emotion']})")

        return "[EPISODIC MEMORY — Past moments with this person]:\n" + "\n".join(lines)

    # ──────────────────────────────────────────────────────────────────────────
    # Full Context Block (injected before LLM call)
    # ──────────────────────────────────────────────────────────────────────────

    def build_memory_context(self, remote_jid: str, current_text: str = "") -> str:
        """
        Build the full memory context block to prepend to the system prompt.
        Combines long-term facts + relevant episodic memories.
        """
        parts = []

        lt = self.format_long_term_context(remote_jid)
        if lt:
            parts.append(lt)

        ep = self.format_episodic_context(remote_jid, current_text)
        if ep:
            parts.append(ep)

        if not parts:
            return ""

        return "\n\n".join(parts)

    # ──────────────────────────────────────────────────────────────────────────
    # Should We Reflect?
    # ──────────────────────────────────────────────────────────────────────────

    def should_reflect(self, remote_jid: str) -> bool:
        count = self.get_short_term_count(remote_jid)
        return count > 0 and count % self.REFLECTION_EVERY_N == 0