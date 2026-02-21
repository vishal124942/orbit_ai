"""
Media Processor — Fast Parallel Pipeline
=========================================

Handles ALL inbound media types with maximum speed via async parallelism:

  audio / voice note → Whisper transcription (whisper-1, fastest model)
  image / photo      → GPT-4o Vision caption
  static sticker     → GPT-4o Vision + emotion/vibe tags
  animated sticker   → Frame extraction → GPT-4o Vision on keyframes
  video              → Parallel: keyframe extraction + audio strip → Vision + Whisper

All results are cached by SHA-256 file hash so the same file is never
processed twice across sessions.

Output: enriched text string injected into the conversation context.
"""

import os
import asyncio
import base64
import hashlib
import subprocess
import tempfile
import json
from typing import Optional, Dict, Tuple
from openai import OpenAI
import cv2
from PIL import Image
import io


# ─────────────────────────────────────────────────────────────────────────────
# Vision prompts tuned per media type
# ─────────────────────────────────────────────────────────────────────────────

VISION_PROMPTS = {
    "image": (
        "Describe this image in 1-2 tight sentences. Focus on: what's happening, "
        "mood, any text visible, faces/expressions if present. Be specific."
    ),
    "sticker_static": (
        "This is a WhatsApp sticker. In one sentence describe: the character/subject, "
        "the exact emotion/vibe (be precise: smug, roasting, crying-laughing, etc.), "
        "and any text on it. Output format: 'Sticker: <description>. Vibe: <vibe>. Emotion: <emotion>.'"
    ),
    "sticker_animated": (
        "These are keyframes from an animated WhatsApp sticker. Describe: what's happening "
        "in the animation, the character's emotion/vibe, any text. Be concise. "
        "Output: 'Animated sticker: <what happens>. Vibe: <vibe>. Emotion: <emotion>.'"
    ),
    "video": (
        "These are keyframes from a video. Describe: what's happening, the mood/tone, "
        "any visible text or faces. 2-3 sentences max."
    ),
}


class MediaProcessor:
    """
    Fast, parallel media processor with file-hash caching.
    """

    def __init__(self, openai_client: OpenAI, sticker_analyzer=None):
        self.client = openai_client
        self.sticker_analyzer = sticker_analyzer
        self._cache: Dict[str, str] = {}  # hash → enriched text
        self._load_cache()

    # ──────────────────────────────────────────────────────────────────────────
    # Cache
    # ──────────────────────────────────────────────────────────────────────────

    def _cache_path(self) -> str:
        os.makedirs("data/media_cache", exist_ok=True)
        return "data/media_cache/processed.json"

    def _load_cache(self):
        try:
            path = self._cache_path()
            if os.path.exists(path):
                with open(path, "r") as f:
                    self._cache = json.load(f)
        except Exception:
            self._cache = {}

    def _save_cache(self):
        try:
            with open(self._cache_path(), "w") as f:
                json.dump(self._cache, f)
        except Exception:
            pass

    def _file_hash(self, path: str) -> Optional[str]:
        try:
            with open(path, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()
        except Exception:
            return None

    # ──────────────────────────────────────────────────────────────────────────
    # Main Entry Point
    # ──────────────────────────────────────────────────────────────────────────

    async def process(self, media_path: str, media_type: str) -> Optional[str]:
        """
        Process any media file and return enriched text for conversation context.

        Args:
            media_path: Absolute path to the media file
            media_type: One of: image, audio, sticker, video

        Returns:
            Enriched text string or None on failure
        """
        if not media_path or not os.path.exists(media_path):
            return None

        # Cache check
        file_hash = self._file_hash(media_path)
        if file_hash and file_hash in self._cache:
            return self._cache[file_hash]

        try:
            if media_type == "audio":
                result = await self._process_audio(media_path)
            elif media_type == "image":
                result = await self._process_image(media_path)
            elif media_type == "sticker":
                result = await self._process_sticker(media_path)
            elif media_type == "video":
                result = await self._process_video(media_path)
            else:
                return None

            # Cache result
            if result and file_hash:
                self._cache[file_hash] = result
                self._save_cache()

            return result

        except Exception as e:
            print(f"[MediaProcessor] ⚠️  Failed ({media_type}): {e}")
            return None

    # ──────────────────────────────────────────────────────────────────────────
    # Audio / Voice Note
    # ──────────────────────────────────────────────────────────────────────────

    async def _process_audio(self, path: str) -> str:
        """Audio transcription — run in thread pool to avoid blocking."""
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, self._transcribe_audio, path)
        return f"[Voice note: \"{result}\"]"

    def _transcribe_audio(self, path: str) -> str:
        """Tries Sarvam AI first, falls back to OpenAI Whisper."""
        converted = self._ensure_audio_format(path)
        try:
            sarvam_key = os.getenv("SARVAM_API_KEY")
            if sarvam_key:
                try:
                    import requests
                    url = "https://api.sarvam.ai/speech-to-text-translate"
                    
                    # Sarvam requires a specific prompt or it infers the language. 'hi-IN' is default
                    payload = {
                        'prompt': '',
                        'model': 'saaras:v2.5'
                    }
                    
                    with open(converted, "rb") as f:
                        files = [('file', (os.path.basename(converted), f, 'audio/mpeg'))]
                        headers = {'api-subscription-key': sarvam_key}
                        
                        response = requests.request("POST", url, headers=headers, data=payload, files=files, timeout=30)
                        
                        if response.status_code == 200:
                            data = response.json()
                            if "transcript" in data:
                                return data["transcript"].strip()
                        else:
                            print(f"[MediaProcessor] Sarvam STT Failed ({response.status_code}): {response.text}")
                except Exception as e:
                    print(f"[MediaProcessor] Sarvam STT Error: {e}")

            # Fallback to OpenAI Whisper
            print("[MediaProcessor] ⚠️ Falling back to OpenAI Whisper for STT")
            with open(converted, "rb") as f:
                resp = self.client.audio.transcriptions.create(
                    model="whisper-1",
                    file=f,
                    response_format="text",
                    language="hi",  # hint: Hindi/Hinglish — faster + more accurate
                )
            return resp.strip()
        finally:
            if converted != path and os.path.exists(converted):
                try:
                    os.unlink(converted)
                except Exception:
                    pass

    def _ensure_audio_format(self, path: str) -> str:
        """Convert audio to mp3 via ffmpeg if not already a supported format."""
        ext = os.path.splitext(path)[1].lower()
        supported = {".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".wav", ".webm", ".ogg"}
        if ext in supported:
            return path

        out = path + "_converted.mp3"
        subprocess.run(
            ["ffmpeg", "-y", "-i", path, "-ar", "16000", "-ac", "1", "-q:a", "0", out],
            capture_output=True, timeout=30
        )
        return out if os.path.exists(out) else path

    # ──────────────────────────────────────────────────────────────────────────
    # Image / Photo
    # ──────────────────────────────────────────────────────────────────────────

    async def _process_image(self, path: str) -> str:
        """GPT-4o vision on photo."""
        loop = asyncio.get_event_loop()
        b64 = await loop.run_in_executor(None, self._encode_image, path)
        resp = await loop.run_in_executor(
            None, self._vision_call, b64, VISION_PROMPTS["image"], "image/jpeg"
        )
        return f"[Photo: {resp}]"

    # ──────────────────────────────────────────────────────────────────────────
    # Sticker — static or animated
    # ──────────────────────────────────────────────────────────────────────────

    async def _process_sticker(self, path: str) -> str:
        """
        Delegate sticker analysis to StickerAnalyzer for deep intelligence 
        and automatic vaulting (Autonomous Learning).
        """
        if not self.sticker_analyzer:
            # Fallback to simple vision if no analyzer
            loop = asyncio.get_event_loop()
            is_animated = await loop.run_in_executor(None, self._is_animated_webp, path)
            if is_animated:
                return await self._process_animated_sticker(path)
            else:
                return await self._process_static_sticker(path)

        # High-intelligence path: Analyzes AND saves to vault automatically
        entry = await self.sticker_analyzer.save_to_vault(path)
        if not entry:
            return "[Sticker: failed to analyze]"

        is_animated = " (ANIMATED)" if entry.get("is_animated") else ""
        vibe = entry.get("vibe", "unknown")
        emotion = entry.get("emotion", "unknown")
        vulgarity = entry.get("vulgarity", "none")
        humor = entry.get("humor_level", "none")
        desc = entry.get("description", "")

        context = f"[Sticker{is_animated}: {vibe} | Emotion: {emotion} | Vulgarity: {vulgarity} | Humor: {humor}]"
        if desc:
            context += f" — {desc[:100]}"
        
        return context

    async def _process_static_sticker(self, path: str) -> str:
        """Static sticker → Vision."""
        loop = asyncio.get_event_loop()
        b64 = await loop.run_in_executor(None, self._encode_image, path)
        resp = await loop.run_in_executor(
            None, self._vision_call, b64, VISION_PROMPTS["sticker_static"], "image/webp"
        )
        vibe = self._extract_vibe_tag(resp)
        return f"[Static sticker: {resp}]{' [vibe:' + vibe + ']' if vibe else ''}"

    async def _process_animated_sticker(self, path: str) -> str:
        """
        Animated sticker: extract 3 keyframes in parallel via ffmpeg,
        send all to Vision in one call as a multi-image message.
        """
        loop = asyncio.get_event_loop()
        frames = await loop.run_in_executor(None, self._extract_webp_frames, path, 3)

        if not frames:
            return await self._process_static_sticker(path)

        resp = await loop.run_in_executor(
            None, self._vision_call_multiframe, frames, VISION_PROMPTS["sticker_animated"]
        )
        vibe = self._extract_vibe_tag(resp)
        return f"[Animated sticker: {resp}]{' [vibe:' + vibe + ']' if vibe else ''}"

    # ──────────────────────────────────────────────────────────────────────────
    # Video
    # ──────────────────────────────────────────────────────────────────────────

    async def _process_video(self, path: str) -> str:
        """
        Parallel: extract keyframes + strip audio → Vision + Whisper simultaneously.
        Combines both results for rich context.
        """
        loop = asyncio.get_event_loop()

        # Run frame extraction and audio extraction in parallel
        frames_task = loop.run_in_executor(None, self._extract_video_frames, path, 4)
        audio_task  = loop.run_in_executor(None, self._extract_video_audio, path)

        frames, audio_path = await asyncio.gather(frames_task, audio_task)

        # Now run Vision and Transcribe in parallel
        vision_task  = loop.run_in_executor(None, self._vision_call_multiframe, frames, VISION_PROMPTS["video"]) if frames else asyncio.coroutine(lambda: None)()
        whisper_task = loop.run_in_executor(None, self._transcribe_audio, audio_path) if audio_path and os.path.exists(audio_path) else asyncio.coroutine(lambda: None)()

        vision_result, whisper_result = await asyncio.gather(
            loop.run_in_executor(None, self._vision_call_multiframe, frames, VISION_PROMPTS["video"]) if frames else asyncio.sleep(0, result=None),
            loop.run_in_executor(None, self._transcribe_audio, audio_path) if audio_path and os.path.exists(audio_path) else asyncio.sleep(0, result=None),
        )

        # Cleanup temp audio
        if audio_path and os.path.exists(audio_path) and audio_path != path:
            try:
                os.unlink(audio_path)
            except Exception:
                pass

        parts = []
        if vision_result:
            parts.append(f"Visual: {vision_result}")
        if whisper_result:
            parts.append(f"Audio: \"{whisper_result}\"")

        combined = " | ".join(parts) if parts else "video (no content extracted)"
        return f"[Video: {combined}]"

    # ──────────────────────────────────────────────────────────────────────────
    # Frame Extraction Helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _extract_video_frames(self, path: str, n_frames: int = 4) -> list:
        """Extract n evenly-spaced keyframes from a video using cv2."""
        frames = []
        try:
            cap = cv2.VideoCapture(path)
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            if total <= 0:
                cap.release()
                return []

            step = max(1, total // (n_frames + 1))
            for i in range(1, n_frames + 1):
                cap.set(cv2.CAP_PROP_POS_FRAMES, min(step * i, total - 1))
                ret, frame = cap.read()
                if ret:
                    # Encode frame to JPEG bytes then base64
                    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
                    b64 = base64.b64encode(buf.tobytes()).decode("utf-8")
                    frames.append(b64)
            cap.release()
        except Exception as e:
            print(f"[MediaProcessor] Frame extraction error: {e}")
        return frames

    def _extract_video_audio(self, path: str) -> Optional[str]:
        """Strip audio track from video to temp mp3 via ffmpeg."""
        try:
            out = tempfile.mktemp(suffix=".mp3")
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", path, "-vn", "-ar", "16000", "-ac", "1",
                 "-q:a", "0", "-t", "120",  # cap at 2 min for speed
                 out],
                capture_output=True, timeout=60
            )
            if result.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
                return out
        except Exception as e:
            print(f"[MediaProcessor] Audio strip error: {e}")
        return None

    def _extract_webp_frames(self, path: str, n: int = 3) -> list:
        """Extract n frames from animated WebP using PIL."""
        frames = []
        try:
            img = Image.open(path)
            total = getattr(img, "n_frames", 1)
            indices = [int(total * i / (n + 1)) for i in range(1, n + 1)]
            for idx in indices:
                try:
                    img.seek(min(idx, total - 1))
                    buf = io.BytesIO()
                    img.convert("RGB").save(buf, format="JPEG", quality=75)
                    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
                    frames.append(b64)
                except Exception:
                    pass
        except Exception as e:
            print(f"[MediaProcessor] WebP frame error: {e}")
        return frames

    def _is_animated_webp(self, path: str) -> bool:
        """Check if a WebP file has multiple frames (is animated)."""
        try:
            img = Image.open(path)
            return getattr(img, "n_frames", 1) > 1
        except Exception:
            return False

    # ──────────────────────────────────────────────────────────────────────────
    # Vision API Calls
    # ──────────────────────────────────────────────────────────────────────────

    def _encode_image(self, path: str, max_dim: int = 1024) -> str:
        """Encode image to base64, downscale if too large for speed."""
        try:
            img = Image.open(path)
            if max(img.size) > max_dim:
                img.thumbnail((max_dim, max_dim), Image.LANCZOS)
            buf = io.BytesIO()
            fmt = "WEBP" if path.lower().endswith(".webp") else "JPEG"
            mime = "image/webp" if fmt == "WEBP" else "image/jpeg"
            img.convert("RGB").save(buf, format="JPEG", quality=80)
            return base64.b64encode(buf.getvalue()).decode("utf-8")
        except Exception:
            with open(path, "rb") as f:
                return base64.b64encode(f.read()).decode("utf-8")

    def _vision_call(self, b64: str, prompt: str, mime: str = "image/jpeg") -> str:
        """Single-image GPT-4o vision call."""
        resp = self.client.chat.completions.create(
            model="gpt-4o",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {
                        "url": f"data:{mime};base64,{b64}",
                        "detail": "low",  # "low" = faster + cheaper, enough for stickers
                    }},
                ],
            }],
            max_tokens=200,
        )
        return resp.choices[0].message.content.strip()

    def _vision_call_multiframe(self, frames: list, prompt: str) -> Optional[str]:
        """Multi-image GPT-4o vision call (for animated stickers / video frames)."""
        if not frames:
            return None
        content = [{"type": "text", "text": prompt}]
        for b64 in frames:
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{b64}",
                    "detail": "low",
                },
            })
        resp = self.client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": content}],
            max_tokens=250,
        )
        return resp.choices[0].message.content.strip()

    # ──────────────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _extract_vibe_tag(self, text: str) -> Optional[str]:
        """Pull the vibe keyword from vision output for sticker matching."""
        text_lower = text.lower()
        vibes = [
            "laughing", "roast", "roasting", "bruh", "hype", "love", "crying",
            "confused", "savage", "chill", "angry", "sad", "cringe", "shocked",
            "happy", "sarcastic", "dead", "wholesome", "flirty",
        ]
        for v in vibes:
            if v in text_lower:
                return v
        return None