"""
LLM-1: Indian Analyzer
Uses Sarvam-M (or OpenAI fallback) to analyze incoming messages.
Output is a vibe/intent signal for LLM-2 — NOT a safety gate.
"""

import os
import json
from openai import OpenAI
from typing import Dict, Any, List, Optional

ANALYSIS_SYSTEM_PROMPT = """You are an expert at analyzing Indian and Hinglish WhatsApp conversations.
You understand Indian communication deeply: banter, gaali between friends, dark humor, business talk,
family conversations, flirting, roasting — the full spectrum.

Analyze the given message(s) and return ONLY a valid JSON object:

{
  "sentiment_score": <float -1.0 to 1.0>,
  "vibe": <"fun" | "flirty" | "angry" | "sad" | "neutral" | "serious" | "banter" | "savage" | "affectionate" | "roast">,
  "toxicity": <"safe" | "banter" | "heavy-banter">,
  "intent": <"casual" | "money" | "emergency" | "question" | "greeting" | "complaint" | "appreciation" | "roast" | "rant">,
  "risk": <"low" | "medium" | "high">,
  "language": <"hindi" | "english" | "hinglish" | "mixed">,
  "requires_sticker": <boolean — true if vibe strongly calls for sticker reply (rare)>,
  "requires_reaction": <boolean — true ONLY for extreme emotion. False for 95% of messages.>,
  "summary": <one-sentence summary of what the user wants/feels>
}

Toxicity is ONLY a signal — it tells LLM-2 how to match the energy:
- "safe"         = standard conversation
- "banter"       = friendly gaali, teasing, roasting between friends — normal Indian chat
- "heavy-banter" = very explicit gaali-heavy convo — match energy fully

Intent risk (for routing decisions):
- "high"   = money or emergency (real-world consequence)
- "medium" = complaint that might need attention
- "low"    = everything else including all banter/gaali

Cultural & Linguistic Dictionary (Hinglish Slang):
- "rand", "randaap", "randi" -> High toxicity (usually heavy-banter/roast)
- "mc", "bc", "bsdk", "lode", "lodu" -> Banter/Heavy-banter depending on tone
- "maa ke lode", "behenchod" -> High toxicity (heavy-banter)
- NOTE: Even in a "greeting" (e.g., "Hi rand"), if toxicity is present, flag it as "heavy-banter" and "roast" intent. Don't let a polite greeting mask the slang.

Cultural notes:
- "bc", "mc", "behen**", "mad**" in friendly context = banter, NOT dangerous
- Gaali + warm tone = close friendship, reciprocate naturally
- ALL styles are valid and should be analyzed accurately, not sanitized
"""


class IndianAnalyzer:
    """
    LLM-1 in the two-LLM stack.
    Produces a vibe/intent analysis that guides the Orchestrator (LLM-2).
    Does NOT gate or block any content — that's not its job.
    """

    def __init__(self, config: Dict, fallback_client: Optional[OpenAI] = None):
        self.config = config
        self.fallback_client = fallback_client
        self.client: Optional[OpenAI] = None
        self.model = "gpt-4o"

        sarvam_cfg = config.get("sarvam", {})
        sarvam_key = os.getenv("SARVAM_API_KEY")
        self.sarvam_enabled = sarvam_cfg.get("enabled", False) or bool(sarvam_key)

        if self.sarvam_enabled and sarvam_key:
            try:
                self.client = OpenAI(
                    api_key=sarvam_key,
                    base_url=sarvam_cfg.get("api_base", "https://api.sarvam.ai/v1"),
                )
                self.model = "sarvam-m"
                print("[IndianAnalyzer] ✅ Sarvam-M ready")
            except Exception as e:
                print(f"[IndianAnalyzer] ⚠️  Sarvam init failed: {e}. Falling back to OpenAI.")

        if not self.client:
            self.client = self.fallback_client
            self.model = config.get("openai", {}).get("model", "gpt-4o")

    async def analyze(self, messages: List[Dict]) -> Dict[str, Any]:
        """
        Analyze a message batch. Returns structured vibe/intent JSON.
        """
        if not messages:
            return self._default_analysis()

        parts = []
        for m in messages:
            name = m.get("pushName") or "User"
            text = m.get("text", "").strip()
            media = m.get("mediaType")
            if text:
                parts.append(f"[{name}]: {text}")
            elif media:
                parts.append(f"[{name}]: [sent a {media}]")

        combined_text = "\n".join(parts).strip()
        if not combined_text or not self.client:
            return self._default_analysis()

        try:
            kwargs = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
                    {"role": "user", "content": f"Analyze:\n\n{combined_text}"},
                ],
                "max_tokens": 400,
                "temperature": 0.1,
            }
            if self.model != "sarvam-m":
                kwargs["response_format"] = {"type": "json_object"}

            response = self.client.chat.completions.create(**kwargs)
            raw_content = response.choices[0].message.content
            cleaned_content = self._clean_json(raw_content)
            result = json.loads(cleaned_content)
            return self._validate(result)

        except json.JSONDecodeError as e:
            print(f"[IndianAnalyzer] JSON error: {e}")
            if hasattr(response, 'choices') and response.choices:
                print(f"[IndianAnalyzer] Raw response: {response.choices[0].message.content}")
            return self._default_analysis()
        except Exception as e:
            print(f"[IndianAnalyzer] Error: {e}")
            return self._default_analysis()

    def _clean_json(self, text: str) -> str:
        """Remove markdown fences if present."""
        text = text.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            # Remove first line if it starts with ```
            if lines[0].strip().startswith("```"):
                lines = lines[1:]
            # Remove last line if it ends with ```
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        return text

    def _validate(self, data: Dict) -> Dict:
        defaults = self._default_analysis()
        for key, val in defaults.items():
            if key not in data:
                data[key] = val
        data["sentiment_score"] = max(-1.0, min(1.0, float(data.get("sentiment_score", 0.0))))
        return data

    def _default_analysis(self) -> Dict[str, Any]:
        return {
            "sentiment_score": 0.0,
            "vibe": "neutral",
            "toxicity": "safe",
            "intent": "casual",
            "risk": "low",
            "language": "hinglish",
            "requires_sticker": False,
            "requires_reaction": False,
            "summary": "General conversation",
        }