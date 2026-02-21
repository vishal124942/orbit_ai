"""
EphemeralMediaProcessor
========================
Processes inbound media (photos, videos, audio, stickers) WITHOUT storing any files.

Pipeline:
  1. Receive file path from WhatsApp bridge (temp file, downloaded by bridge)
  2. Read into memory / process via ffmpeg/opencv
  3. Call AI (Whisper / GPT-4o Vision)
  4. Return enriched text description
  5. DELETE the temp file immediately
  6. Cache description by SHA-256 hash → PostgreSQL (no filesystem cache)

Storage savings vs original:
  ❌ No data/media/ directory (was storing every photo/video/audio)
  ❌ No data/media_cache/processed.json (now in PostgreSQL)
  ✅ Stickers still go to stickers vault (read-only reference data)
  ✅ TTS output files kept (they're outbound, useful to cache)

Per-user TTS cache cleanup:
  - Files older than 7 days are pruned automatically
  - Max 50MB per user (configurable)
"""

import os
import asyncio
import base64
import hashlib
import subprocess
import tempfile
from typing import Optional, Dict, List
from openai import OpenAI
import io

try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


VISION_PROMPTS = {
    "image": (
        "Describe this image in 1-2 tight sentences. Focus on: what's happening, "
        "mood, any text visible, faces/expressions if present. Be specific."
    ),
    "sticker_static": (
        "This is a WhatsApp sticker. In one sentence describe: the character/subject, "
        "the exact emotion/vibe (be precise: smug, roasting, crying-laughing, etc.), "
        "and any text on it. Output: 'Sticker: <desc>. Vibe: <vibe>. Emotion: <emotion>.'"
    ),
    "sticker_animated": (
        "These are keyframes from an animated WhatsApp sticker. Describe: what's happening, "
        "the emotion/vibe, any text. Output: 'Animated sticker: <what happens>. Vibe: <vibe>.'"
    ),
    "video": (
        "These are keyframes from a video. Describe: what's happening, mood/tone, "
        "any visible text or faces. 2-3 sentences max."
    ),
}


class EphemeralMediaProcessor:
    """
    Zero-persistence media processor.
    Processes media in-memory/temp files, stores ONLY text description.
    The description is cached in PostgreSQL by file hash — no filesystem cache.
    """

    def __init__(self, openai_client: OpenAI, platform_db=None, sticker_analyzer=None):
        self.client = openai_client
        self.db = platform_db          # PlatformDB for hash→description cache
        self.sticker_analyzer = sticker_analyzer
        self._in_memory_cache: Dict[str, str] = {}   # Fast in-process cache

    # ── Main entry point ──────────────────────────────────────────────────────

    async def process(self, media_path: str, media_type: str) -> Optional[str]:
        """
        Process any media file. Returns enriched text. Deletes source file when done.
        media_type: "audio" | "image" | "sticker" | "video"
        """
        if not media_path or not os.path.exists(media_path):
            return None

        file_hash = self._hash_file(media_path)

        # 1. Check in-process memory cache
        if file_hash and file_hash in self._in_memory_cache:
            self._delete_safe(media_path)
            return self._in_memory_cache[file_hash]

        # 2. Check PostgreSQL cache
        if file_hash and self.db:
            cached = await self._pg_get(file_hash)
            if cached:
                self._in_memory_cache[file_hash] = cached
                self._delete_safe(media_path)
                return cached

        # 3. Process
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
                result = None

            # 4. Cache result
            if result and file_hash:
                self._in_memory_cache[file_hash] = result
                if self.db:
                    asyncio.create_task(self._pg_save(file_hash, media_type, result))

        except Exception as e:
            print(f"[MediaProcessor] ⚠️  Failed ({media_type}): {e}")
            result = None
        finally:
            # 5. DELETE the file — we only need the description
            self._delete_safe(media_path)

        return result

    def _delete_safe(self, path: str):
        """Silently delete a file. Used to clean up temp media files."""
        try:
            if os.path.exists(path):
                os.unlink(path)
        except Exception:
            pass

    def _hash_file(self, path: str) -> Optional[str]:
        """SHA-256 of file contents for cache key."""
        try:
            h = hashlib.sha256()
            with open(path, "rb") as f:
                while chunk := f.read(65536):
                    h.update(chunk)
            return h.hexdigest()
        except Exception:
            return None

    async def _pg_get(self, file_hash: str) -> Optional[str]:
        try:
            return await self.db.get_media_description(file_hash)
        except Exception:
            return None

    async def _pg_save(self, file_hash: str, media_type: str, description: str):
        try:
            await self.db.save_media_description(file_hash, media_type, description)
        except Exception:
            pass

    # ── Audio ─────────────────────────────────────────────────────────────────

    async def _process_audio(self, path: str) -> str:
        loop = asyncio.get_event_loop()
        text = await loop.run_in_executor(None, self._whisper_transcribe, path)
        return f'[Voice note: "{text}"]'

    def _whisper_transcribe(self, path: str) -> str:
        converted = self._to_mp3_temp(path)
        try:
            with open(converted, "rb") as f:
                resp = self.client.audio.transcriptions.create(
                    model="whisper-1",
                    file=f,
                    response_format="text",
                    language="hi",
                )
            return resp.strip()
        finally:
            # Always clean up the converted temp file
            if converted != path:
                self._delete_safe(converted)

    def _to_mp3_temp(self, path: str) -> str:
        """Convert audio to mp3 in a temp file if needed."""
        ext = os.path.splitext(path)[1].lower()
        if ext in {".mp3", ".m4a", ".wav", ".webm", ".ogg", ".mp4", ".mpeg"}:
            return path
        tmp = tempfile.mktemp(suffix=".mp3")
        subprocess.run(
            ["ffmpeg", "-y", "-i", path, "-ar", "16000", "-ac", "1", "-q:a", "0", tmp],
            capture_output=True, timeout=30
        )
        return tmp if os.path.exists(tmp) else path

    # ── Image ─────────────────────────────────────────────────────────────────

    async def _process_image(self, path: str) -> str:
        loop = asyncio.get_event_loop()
        b64 = await loop.run_in_executor(None, self._encode_image, path)
        desc = await loop.run_in_executor(
            None, self._vision_single, b64, VISION_PROMPTS["image"]
        )
        return f"[Photo: {desc}]"

    # ── Sticker ───────────────────────────────────────────────────────────────

    async def _process_sticker(self, path: str) -> str:
        """
        Sticker processing:
        - If StickerAnalyzer available: vault it (the vault IS the sticker store)
        - Otherwise: analyze in-memory, no file save
        
        CRITICAL: We do NOT copy sticker to data/media. 
        The sticker vault (data/stickers/) is the ONLY place it gets stored.
        """
        loop = asyncio.get_event_loop()

        if self.sticker_analyzer:
            # Vault it — sticker_analyzer.save_to_vault copies to data/stickers/
            # That's the one and only copy. The original temp file gets deleted by our finally block.
            entry = await self.sticker_analyzer.save_to_vault(path)
            if entry:
                is_anim = " (ANIMATED)" if entry.get("is_animated") else ""
                return (
                    f"[Sticker{is_anim}: {entry.get('vibe','?')} | "
                    f"Emotion: {entry.get('emotion','?')} | "
                    f"{entry.get('description','')[:100]}]"
                )

        # Fallback: analyze in-memory, don't save anywhere
        is_animated = await loop.run_in_executor(None, self._is_animated_webp, path)
        if is_animated:
            frames = await loop.run_in_executor(None, self._extract_webp_frames, path, 3)
            desc = await loop.run_in_executor(
                None, self._vision_multi, frames, VISION_PROMPTS["sticker_animated"]
            )
        else:
            b64 = await loop.run_in_executor(None, self._encode_image, path)
            desc = await loop.run_in_executor(
                None, self._vision_single, b64, VISION_PROMPTS["sticker_static"], "image/webp"
            )
        return f"[Sticker: {desc}]"

    # ── Video ─────────────────────────────────────────────────────────────────

    async def _process_video(self, path: str) -> str:
        """
        Parallel: extract frames in-memory + strip audio to temp → Vision + Whisper.
        All temp files are cleaned up immediately after use.
        NO video data is persisted.
        """
        loop = asyncio.get_event_loop()

        audio_tmp = tempfile.mktemp(suffix=".mp3")

        frames_task = loop.run_in_executor(None, self._extract_video_frames, path, 4)
        audio_task = loop.run_in_executor(None, self._extract_video_audio, path, audio_tmp)

        frames, audio_ok = await asyncio.gather(frames_task, audio_task)

        vision_task = (
            loop.run_in_executor(None, self._vision_multi, frames, VISION_PROMPTS["video"])
            if frames else asyncio.sleep(0, result=None)
        )
        whisper_task = (
            loop.run_in_executor(None, self._whisper_transcribe, audio_tmp)
            if audio_ok and os.path.exists(audio_tmp) else asyncio.sleep(0, result=None)
        )

        vision_res, whisper_res = await asyncio.gather(vision_task, whisper_task)

        # Clean up temp audio
        self._delete_safe(audio_tmp)

        parts = []
        if vision_res:
            parts.append(f"Visual: {vision_res}")
        if whisper_res:
            parts.append(f'Audio: "{whisper_res}"')

        combined = " | ".join(parts) if parts else "video (no content extracted)"
        return f"[Video: {combined}]"

    # ── Frame extraction helpers ───────────────────────────────────────────────

    def _extract_video_frames(self, path: str, n: int = 4) -> List[str]:
        """Extract n frames from video → list of base64 strings (in-memory only)."""
        frames = []
        if not HAS_CV2:
            return frames
        try:
            cap = cv2.VideoCapture(path)
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            if total <= 0:
                cap.release()
                return []
            step = max(1, total // (n + 1))
            for i in range(1, n + 1):
                cap.set(cv2.CAP_PROP_POS_FRAMES, min(step * i, total - 1))
                ret, frame = cap.read()
                if ret:
                    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                    frames.append(base64.b64encode(buf.tobytes()).decode())
            cap.release()
        except Exception as e:
            print(f"[MediaProcessor] Frame extract error: {e}")
        return frames

    def _extract_video_audio(self, path: str, out_path: str) -> bool:
        """Strip audio to temp file. Caller is responsible for deleting out_path."""
        try:
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", path, "-vn", "-ar", "16000", "-ac", "1",
                 "-q:a", "0", "-t", "120", out_path],
                capture_output=True, timeout=60
            )
            return result.returncode == 0 and os.path.getsize(out_path) > 0
        except Exception:
            return False

    def _extract_webp_frames(self, path: str, n: int = 3) -> List[str]:
        """Extract frames from animated WebP → base64 list (in-memory)."""
        frames = []
        if not HAS_PIL:
            return frames
        try:
            img = Image.open(path)
            total = getattr(img, "n_frames", 1)
            indices = [int(total * i / (n + 1)) for i in range(1, n + 1)]
            for idx in indices:
                img.seek(min(idx, total - 1))
                buf = io.BytesIO()
                img.convert("RGB").save(buf, format="JPEG", quality=70)
                frames.append(base64.b64encode(buf.getvalue()).decode())
        except Exception:
            pass
        return frames

    def _is_animated_webp(self, path: str) -> bool:
        if not HAS_PIL:
            return False
        try:
            img = Image.open(path)
            return getattr(img, "n_frames", 1) > 1
        except Exception:
            return False

    # ── Vision helpers ────────────────────────────────────────────────────────

    def _encode_image(self, path: str, max_dim: int = 800) -> str:
        """Encode image to JPEG base64, resizing if needed. Never saves to disk."""
        if HAS_PIL:
            try:
                img = Image.open(path)
                if max(img.size) > max_dim:
                    img.thumbnail((max_dim, max_dim), Image.LANCZOS)
                buf = io.BytesIO()
                img.convert("RGB").save(buf, format="JPEG", quality=80)
                return base64.b64encode(buf.getvalue()).decode()
            except Exception:
                pass
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode()

    def _vision_single(self, b64: str, prompt: str, mime: str = "image/jpeg") -> str:
        resp = self.client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {
                    "url": f"data:{mime};base64,{b64}",
                    "detail": "low",
                }},
            ]}],
            max_tokens=200,
        )
        return resp.choices[0].message.content.strip()

    def _vision_multi(self, frames: List[str], prompt: str) -> Optional[str]:
        if not frames:
            return None
        content = [{"type": "text", "text": prompt}]
        for b64 in frames:
            content.append({"type": "image_url", "image_url": {
                "url": f"data:image/jpeg;base64,{b64}",
                "detail": "low",
            }})
        resp = self.client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": content}],
            max_tokens=250,
        )
        return resp.choices[0].message.content.strip()


# ── TTS Cache Janitor ─────────────────────────────────────────────────────────

async def prune_tts_cache(data_dir: str, max_age_days: int = 7, max_size_mb: int = 50):
    """
    Clean up TTS audio files for a user.
    Runs as a background task — won't block anything.
    """
    import time
    tts_dir = os.path.join(data_dir, "tts")
    if not os.path.exists(tts_dir):
        return

    now = time.time()
    max_age_sec = max_age_days * 86400
    total_bytes = 0
    files = []

    for fname in os.listdir(tts_dir):
        fpath = os.path.join(tts_dir, fname)
        if not os.path.isfile(fpath):
            continue
        stat = os.stat(fpath)
        files.append((stat.st_atime, fpath, stat.st_size))
        total_bytes += stat.st_size

    # Sort by last access time (oldest first)
    files.sort()

    for atime, fpath, fsize in files:
        should_delete = False
        # Too old
        if now - atime > max_age_sec:
            should_delete = True
        # Over size limit (delete oldest first)
        elif total_bytes > max_size_mb * 1024 * 1024:
            should_delete = True

        if should_delete:
            try:
                os.unlink(fpath)
                total_bytes -= fsize
            except Exception:
                pass