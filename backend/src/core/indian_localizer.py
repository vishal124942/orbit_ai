"""
Indian Localizer
Rewrites LLM-2's planned reply in natural Hinglish / WhatsApp style.
No content filtering — raw language, gaali, slang all preserved.
"""

import os
from openai import OpenAI
from typing import Dict, Optional


LOCALIZE_SYSTEM_PROMPT = """You are a WhatsApp message style adapter.
Your ONLY job: rewrite the input into natural, conversational WhatsApp text.

RULES:
1.  **MATCH THE LANGUAGE**: If language=english, keep it English. If language=hinglish, write natural Hinglish. If language=hindi, write Hindi in Roman script.
2.  **MATCH THE TONE**: 
    -   Vibe "neutral" or "professional" → Clean, crisp, no slang. ("Sure, noted.", "Got it, will do.")
    -   Vibe "casual" or "friendly" → Light, relaxed. ("Haan cool", "Sure thing")
    -   Vibe "banter" → Playful but not aggressive. 
    -   Vibe "sad" or "serious" → Empathetic, calm, no jokes.
3.  **NO EXAGGERATION**: Do NOT add slang, fillers, or gaali that weren't in the input.
4.  **SHORT**: Keep it 1-3 lines. WhatsApp messages are brief.
5.  **NO ASSISTANT SPEAK**: Never say "How can I help", "I understand your concern".
6.  **EMOJI**: Max 1 emoji, only if the vibe is casual or friendly. None for professional/neutral.
7.  **PRESERVE MEANING**: Don't change what the message says, only how it sounds.

Output ONLY the rewritten text, nothing else.
"""



class IndianLocalizer:
    """
    Post-planning Hinglish rewriter.
    Preserves full language authenticity — no word substitution or softening.
    """

    def __init__(self, config: Dict, fallback_client: Optional[OpenAI] = None):
        self.config = config
        self.fallback_client = fallback_client
        self.client: Optional[OpenAI] = None
        self.model = config.get("openai", {}).get("model", "gpt-4o")

        sarvam_cfg = config.get("sarvam", {})
        sarvam_key = os.getenv("SARVAM_API_KEY")
        self.enabled = sarvam_cfg.get("enabled", False) or bool(sarvam_key)

        if self.enabled and sarvam_key:
            try:
                self.client = OpenAI(
                    api_key=sarvam_key,
                    base_url=sarvam_cfg.get("api_base", "https://api.sarvam.ai/v1"),
                )
                self.model = "sarvam-m"
                print("[IndianLocalizer] ✅ Sarvam-M ready for localization")
            except Exception as e:
                print(f"[IndianLocalizer] ⚠️  Sarvam init failed: {e}")

        if not self.client:
            self.client = self.fallback_client
            self.model = config.get("openai", {}).get("model", "gpt-4o")

    async def localize(self, text: str, vibe: str, language: str) -> str:
        """
        Rewrite text in Hinglish WhatsApp style.
        Falls back to original text on any error.
        """
        if not text or not text.strip():
            return text

        # Skip for pure English conversations
        if language == "english":
            return text

        if not self.client:
            return text

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": LOCALIZE_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"Vibe: {vibe}\n"
                            f"Language: {language}\n\n"
                            f"Rewrite:\n{text}"
                        ),
                    },
                ],
                max_tokens=600,
                temperature=0.7,
            )
            localized = response.choices[0].message.content.strip()
            return localized if localized else text

        except Exception as e:
            print(f"[IndianLocalizer] ⚠️  Failed: {e}. Using original.")
            return text