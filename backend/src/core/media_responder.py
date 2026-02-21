"""
Media Responder — Outbound Media Generation
=============================================

Decides WHAT kind of media to respond with and generates it.

Response types (LLM-2 can request any of these in the plan):
  text      → plain WhatsApp text (always available)
  audio     → TTS voice note (OpenAI TTS API, tts-1 for speed)
  sticker   → from local sticker vault
  image     → from local image vault or generated (DALL-E optional)
  reaction  → emoji reaction on message

The Orchestrator picks the response_type in its action plan JSON.
This class handles the generation and file management.
"""

import os
import asyncio
import hashlib
import tempfile
import subprocess
import boto3
from typing import Optional, Dict, Tuple
from openai import OpenAI


# TTS voices — pick per conversation vibe
VOICE_MAP = {
    # vibe → OpenAI TTS voice
    "default":       "onyx",    # deep, warm, versatile
    "fun":           "alloy",   # upbeat, friendly
    "banter":        "fable",   # expressive, personality
    "roast":         "onyx",    # deep, confident
    "affectionate":  "nova",    # warm, soft
    "sad":           "shimmer", # gentle, empathetic
    "serious":       "echo",    # clear, professional
    "flirty":        "nova",
    "angry":         "onyx",
    "savage":        "fable",
}

# If audio track ≤ this many chars, prefer voice note. More = prefer text.
AUDIO_TEXT_CHAR_LIMIT = 280


class MediaResponder:
    """
    Handles outbound media generation for Orbit's responses.
    """

    def __init__(self, openai_client: OpenAI, config: Dict):
        self.client = openai_client
        self.config = config
        self._tts_cache: Dict[str, str] = {}  # text_hash → file path or URL

        self.tts_dir = "data/tts"
        os.makedirs(self.tts_dir, exist_ok=True)
        
        # R2 / S3 Config
        self.r2_bucket = os.getenv("R2_BUCKET_NAME")
        if self.r2_bucket:
            self.s3_client = boto3.client(
                's3',
                endpoint_url=os.getenv("R2_ENDPOINT"),
                aws_access_key_id=os.getenv("R2_ACCESS_KEY_ID"),
                aws_secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY"),
                region_name=os.getenv("R2_REGION", "auto")
            )
            self.r2_public_url = os.getenv("R2_PUBLIC_URL", "").rstrip("/")

    # ──────────────────────────────────────────────────────────────────────────
    # Decision: What media type should the response be?
    # ──────────────────────────────────────────────────────────────────────────

    def recommend_response_type(
        self,
        analysis: Dict,
        plan: Dict,
        inbound_media_type: Optional[str] = None,
    ) -> str:
        """
        Given the analysis + plan, recommend the best response media type.
        Returns: "text" | "audio" | "sticker" | "sticker+text" | "text+sticker"

        Rules (in priority order):
        1. Plan explicitly sets response_type → honour it
        2. Inbound was a voice note → respond with voice note (reciprocate)
        3. Vibe is banter/fun/roast and text ≤ limit → voice note adds personality
        4. Default → text (+ sticker if sticker_vibe is set)
        """
        explicit = plan.get("response_type", "").strip()
        if explicit and explicit != "auto":
            return explicit

        # Reciprocate voice note with voice note (Priority 1)
        if inbound_media_type == "audio":
            return "audio"

        # Auto rule removed: Banter/Fun no longer automatically triggers audio
        # LLM-2 must now EXPLICITLY request "audio" in the plan for it to trigger
        
        return "text"

    # ──────────────────────────────────────────────────────────────────────────
    # TTS — Text to Voice Note
    # ──────────────────────────────────────────────────────────────────────────

    async def generate_voice_note(self, text: str, vibe: str = "default") -> Optional[str]:
        """
        Generate a voice note (OGG/opus) from text using OpenAI TTS.
        Returns file path to the audio file, or None on failure.
        Caches by text hash — identical messages never re-generated.
        """
        if not text or not text.strip():
            return None

        text = text[:4096]  # API hard limit
        text_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
        cache_key = f"{text_hash}_{vibe}"

        if cache_key in self._tts_cache:
            cached = self._tts_cache[cache_key]
            # Since URLs don't exist locally, just return if it starts with http
            if cached.startswith("http") or os.path.exists(cached):
                return cached

        voice = VOICE_MAP.get(vibe, VOICE_MAP["default"])
        out_path = os.path.join(self.tts_dir, f"{cache_key}.ogg")

        try:
            loop = asyncio.get_event_loop()
            mp3_path = os.path.join(self.tts_dir, f"{cache_key}.mp3")

            # Generate MP3 in thread pool (blocking API call)
            await loop.run_in_executor(
                None,
                self._tts_api_call,
                text, voice, mp3_path
            )

            if not os.path.exists(mp3_path):
                return None

            # Convert MP3 → OGG/opus (WhatsApp PTT format) via ffmpeg
            result = await loop.run_in_executor(
                None,
                self._convert_to_ogg,
                mp3_path, out_path
            )

            if os.path.exists(mp3_path):
                os.unlink(mp3_path)

            if result and os.path.exists(out_path):
                # Upload to R2 if configured
                if self.r2_bucket and getattr(self, "s3_client", None) and getattr(self, "r2_public_url", None):
                    object_name = f"tts/{cache_key}.ogg"
                    try:
                        loop = asyncio.get_event_loop()
                        await loop.run_in_executor(
                            None,
                            self._upload_to_s3,
                            out_path, self.r2_bucket, object_name
                        )
                        # Remove local temp file
                        os.unlink(out_path)
                        public_url = f"{self.r2_public_url}/{object_name}"
                        self._tts_cache[cache_key] = public_url
                        return public_url
                    except Exception as e:
                        print(f"[MediaResponder] R2 TTS upload failed: {e}")
                
                # Fallback: serve locally
                self._tts_cache[cache_key] = out_path
                return out_path

        except Exception as e:
            print(f"[MediaResponder] TTS error: {e}")

        return None

    def _tts_api_call(self, text: str, voice: str, out_path: str):
        """Sync TTS API call — runs in executor."""
        response = self.client.audio.speech.create(
            model="tts-1",          # tts-1 = fast, tts-1-hd = higher quality
            voice=voice,
            input=text,
            response_format="mp3",
            speed=1.05,             # Slightly faster = more natural Indian speech cadence
        )
        response.stream_to_file(out_path)

    def _convert_to_ogg(self, mp3_path: str, ogg_path: str) -> bool:
        """Convert MP3 to OGG/opus for WhatsApp PTT via ffmpeg."""
        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-y", "-i", mp3_path,
                    "-c:a", "libopus",
                    "-b:a", "32k",    # Low bitrate — voice notes don't need more
                    "-ar", "48000",   # WhatsApp opus standard
                    "-ac", "1",
                    ogg_path,
                ],
                capture_output=True,
                timeout=30,
            )
            return result.returncode == 0
        except Exception as e:
            print(f"[MediaResponder] OGG conversion error: {e}")
            return False

    def _upload_to_s3(self, file_path: str, bucket: str, object_name: str):
        """Uploads a file to S3/R2 synchronously."""
        self.s3_client.upload_file(file_path, bucket, object_name)

    # ──────────────────────────────────────────────────────────────────────────
    # Voice Map Selection
    # ──────────────────────────────────────────────────────────────────────────

    def get_voice_for_vibe(self, vibe: str) -> str:
        return VOICE_MAP.get(vibe, VOICE_MAP["default"])