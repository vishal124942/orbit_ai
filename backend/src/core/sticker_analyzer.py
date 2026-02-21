import os
import base64
import json
import hashlib
import shutil
from typing import Dict, Optional, List
from openai import OpenAI
from PIL import Image
import subprocess

class StickerAnalyzer:
    """
    Enhanced sticker analyzer that handles:
    - Animated stickers (GIF-like video stickers)
    - Multi-frame analysis
    - Vulgarity detection
    - Humor level assessment
    - Context and emotion detection
    """
    
    def __init__(self, openai_client: OpenAI):
        self.client = openai_client
        self.vault_dir = "data/stickers"
        self.index_path = os.path.join(self.vault_dir, "stickers.json")
        os.makedirs(self.vault_dir, exist_ok=True)
    
    def is_animated(self, sticker_path: str) -> bool:
        """Check if sticker is animated"""
        try:
            # Try to open as image
            with Image.open(sticker_path) as img:
                # WebP can be animated
                if hasattr(img, 'n_frames') and img.n_frames > 1:
                    return True
            
            # Check if it's actually a video file (some animated stickers are mp4)
            if sticker_path.endswith('.mp4'):
                return True
            
            return False
        except:
            return False
    
    def extract_frames(self, sticker_path: str, num_frames: int = 3) -> List[str]:
        """
        Extract key frames from animated sticker
        Returns list of base64-encoded frame images
        """
        frames = []
        
        try:
            if sticker_path.endswith('.webp'):
                # Extract frames from animated WebP
                with Image.open(sticker_path) as img:
                    total_frames = getattr(img, 'n_frames', 1)
                    
                    if total_frames > 1:
                        # Get first, middle, and last frame
                        frame_indices = [0, total_frames // 2, total_frames - 1]
                        
                        for idx in frame_indices[:num_frames]:
                            img.seek(idx)
                            # Convert frame to RGB and encode
                            frame_rgb = img.convert('RGB')
                            from io import BytesIO
                            buffer = BytesIO()
                            frame_rgb.save(buffer, format='JPEG')
                            frame_data = base64.b64encode(buffer.getvalue()).decode('utf-8')
                            frames.append(frame_data)
                    else:
                        # Static image
                        img_rgb = img.convert('RGB')
                        from io import BytesIO
                        buffer = BytesIO()
                        img_rgb.save(buffer, format='JPEG')
                        frame_data = base64.b64encode(buffer.getvalue()).decode('utf-8')
                        frames.append(frame_data)
            
            elif sticker_path.endswith('.mp4'):
                # Extract frames from video using ffmpeg
                temp_dir = "/tmp/sticker_frames"
                os.makedirs(temp_dir, exist_ok=True)
                
                # Extract 3 frames at 0%, 50%, 100% of video
                frame_paths = []
                for i, percent in enumerate([0, 50, 100]):
                    output_path = os.path.join(temp_dir, f"frame_{i}.jpg")
                    cmd = [
                        'ffmpeg', '-i', sticker_path,
                        '-vf', f'select=eq(n\\,0)',  # First frame
                        '-vframes', '1',
                        '-y', output_path
                    ]
                    
                    try:
                        subprocess.run(cmd, capture_output=True, timeout=5)
                        if os.path.exists(output_path):
                            frame_paths.append(output_path)
                    except:
                        pass
                
                # Encode frames
                for frame_path in frame_paths[:num_frames]:
                    with open(frame_path, 'rb') as f:
                        frame_data = base64.b64encode(f.read()).decode('utf-8')
                        frames.append(frame_data)
                    os.remove(frame_path)
        
        except Exception as e:
            print(f"Warning: Frame extraction failed: {e}")
            # Fallback: treat as static image
            try:
                with open(sticker_path, 'rb') as f:
                    frame_data = base64.b64encode(f.read()).decode('utf-8')
                    frames.append(frame_data)
            except:
                pass
        
        return frames
    
    async def analyze_sticker(self, sticker_path: str) -> Dict:
        """
        Comprehensive sticker analysis including:
        - Emotion/vibe
        - Vulgarity level
        - Humor type
        - Context/meaning
        - Animation description (if animated)
        """
        
        is_animated = self.is_animated(sticker_path)
        
        if is_animated:
            frames = self.extract_frames(sticker_path, num_frames=3)
            analysis_type = "animated sticker"
            
            # Build multi-frame analysis prompt
            content = [
                {
                    "type": "text",
                    "text": """Analyze this animated sticker comprehensively.

Provide a JSON response with:
{
  "emotion": "primary emotion (happy, sad, angry, laughing, roasting, love, celebration, crying, shocked, confused, etc.)",
  "vibe": "2-3 word description (e.g., 'funny laughing', 'angry shouting', 'vulgar gesture', 'romantic love')",
  "vulgarity": "none|mild|moderate|high",
  "vulgarity_type": "if vulgar, specify: middle_finger, sexual_gesture, offensive_text, crude_humor, etc. Otherwise null",
  "humor_level": "none|low|medium|high",
  "humor_type": "sarcastic|slapstick|dark|wholesome|roasting|meme|etc or null",
  "animation_description": "describe what happens in the animation (2-3 sentences)",
  "context": "when to use this sticker (e.g., 'roasting friends', 'celebrating victory', 'expressing anger')",
  "language_detected": "any text visible in sticker (Hindi, English, etc.) or null",
  "cultural_context": "Indian|Western|Universal|Anime|etc",
  "intensity": "low|medium|high (how intense the emotion/action is)"
}

Be culturally aware - understand Indian slang, Hinglish, and gestures."""
                }
            ]
            
            # Add frames
            for i, frame in enumerate(frames[:3]):
                content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{frame}",
                        "detail": "low"
                    }
                })
                if i == 0:
                    content.append({"type": "text", "text": f"Frame 1 (start):"})
                elif i == 1:
                    content.append({"type": "text", "text": f"Frame 2 (middle):"})
                else:
                    content.append({"type": "text", "text": f"Frame 3 (end):"})
        
        else:
            # Static sticker
            analysis_type = "static sticker"
            
            with open(sticker_path, 'rb') as f:
                image_data = base64.b64encode(f.read()).decode('utf-8')
            
            content = [
                {
                    "type": "text",
                    "text": """Analyze this sticker comprehensively.

Provide a JSON response with:
{
  "emotion": "primary emotion",
  "vibe": "2-3 word description",
  "vulgarity": "none|mild|moderate|high",
  "vulgarity_type": "if vulgar, specify type. Otherwise null",
  "humor_level": "none|low|medium|high",
  "humor_type": "type of humor or null",
  "description": "what the sticker shows (2-3 sentences)",
  "context": "when to use this",
  "language_detected": "any text visible or null",
  "cultural_context": "cultural origin",
  "intensity": "low|medium|high"
}

Be culturally aware - understand Indian slang, Hinglish, and gestures."""
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/webp;base64,{image_data}"
                    }
                }
            ]
        
        # Analyze with GPT-4o
        try:
            response = self.client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {
                        "role": "user",
                        "content": content
                    }
                ],
                response_format={"type": "json_object"},
                max_tokens=300
            )
            
            analysis = json.loads(response.choices[0].message.content)
            
            # Add metadata
            analysis["is_animated"] = is_animated
            analysis["analysis_type"] = analysis_type
            
            return analysis
            
        except Exception as e:
            print(f"Warning: Sticker analysis failed: {e}")
            # Fallback basic analysis
            return {
                "emotion": "unknown",
                "vibe": "unanalyzed sticker",
                "vulgarity": "unknown",
                "vulgarity_type": None,
                "humor_level": "unknown",
                "humor_type": None,
                "description": "analysis failed",
                "context": "general use",
                "language_detected": None,
                "cultural_context": "unknown",
                "intensity": "medium",
                "is_animated": is_animated,
                "error": str(e)
            }
    
    async def save_to_vault(self, sticker_path: str) -> Optional[Dict]:
        """Save sticker to vault with comprehensive analysis"""
        
        if not os.path.exists(sticker_path):
            return None
        
        # Calculate hash
        with open(sticker_path, 'rb') as f:
            file_hash = hashlib.sha256(f.read()).digest().hex()
        
        vault_path = os.path.join(self.vault_dir, f"{file_hash}.webp")
        
        # Check if already in vault
        if os.path.exists(vault_path):
            # Load existing analysis
            index = self._load_index()
            existing = next((s for s in index if s['hash'] == file_hash), None)
            if existing:
                return existing
        
        # Copy to vault
        shutil.copy(sticker_path, vault_path)
        
        # Analyze
        analysis = await self.analyze_sticker(vault_path)
        
        # Create index entry
        entry = {
            "path": vault_path,
            "hash": file_hash,
            "emotion": analysis.get("emotion", "unknown"),
            "vibe": analysis.get("vibe", "unknown"),
            "vulgarity": analysis.get("vulgarity", "none"),
            "vulgarity_type": analysis.get("vulgarity_type"),
            "humor_level": analysis.get("humor_level", "none"),
            "humor_type": analysis.get("humor_type"),
            "description": analysis.get("description") or analysis.get("animation_description", ""),
            "context": analysis.get("context", "general"),
            "language": analysis.get("language_detected"),
            "cultural": analysis.get("cultural_context", "universal"),
            "intensity": analysis.get("intensity", "medium"),
            "is_animated": analysis.get("is_animated", False),
            "full_analysis": analysis
        }
        
        # Update index
        index = self._load_index()
        index.append(entry)
        self._save_index(index)
        
        return entry
    
    def search_stickers(
        self, 
        vibe: str = None, 
        emotion: str = None,
        include_vulgar: bool = True,
        max_vulgarity: str = "high",
        humor_only: bool = False,
        animated_only: bool = False
    ) -> List[Dict]:
        """
        Advanced sticker search with filters
        
        Args:
            vibe: Keywords to search in vibe/description
            emotion: Specific emotion to match
            include_vulgar: Whether to include vulgar stickers
            max_vulgarity: Maximum vulgarity level (none, mild, moderate, high)
            humor_only: Only return funny stickers
            animated_only: Only return animated stickers
        """
        
        index = self._load_index()
        results = index
        
        # Filter by vulgarity
        if not include_vulgar:
            results = [s for s in results if s.get('vulgarity', 'none') == 'none']
        else:
            # Filter by max vulgarity level
            vulgarity_levels = ['none', 'mild', 'moderate', 'high']
            try:
                max_level_idx = vulgarity_levels.index(max_vulgarity)
            except ValueError:
                max_level_idx = len(vulgarity_levels) - 1  # Default to allowing everything if invalid max

            results = [
                s for s in results 
                if s.get('vulgarity', 'none') in vulgarity_levels and 
                   vulgarity_levels.index(s.get('vulgarity', 'none')) <= max_level_idx
            ]
        
        # Filter by humor
        if humor_only:
            results = [
                s for s in results 
                if s.get('humor_level', 'none') in ['medium', 'high']
            ]
        
        # Filter by animation
        if animated_only:
            results = [s for s in results if s.get('is_animated', False)]
        
        # Filter by emotion
        if emotion:
            emotion_lower = emotion.lower()
            results = [
                s for s in results 
                if emotion_lower in s.get('emotion', '').lower()
            ]
        
        # Search by vibe/keywords
        if vibe:
            vibe_lower = vibe.lower()
            results = [
                s for s in results
                if (vibe_lower in s.get('vibe', '').lower() or
                    vibe_lower in s.get('description', '').lower() or
                    vibe_lower in s.get('context', '').lower() or
                    vibe_lower in s.get('emotion', '').lower())
            ]
        
        return results[:20]  # Return top 20 matches
    
    def get_sticker_intelligence(self, file_hash: str) -> Optional[Dict]:
        """Get full analysis for a specific sticker"""
        index = self._load_index()
        sticker = next((s for s in index if s['hash'] == file_hash), None)
        return sticker.get('full_analysis') if sticker else None
    
    def _load_index(self) -> List[Dict]:
        """Load sticker index"""
        if os.path.exists(self.index_path):
            try:
                with open(self.index_path, 'r') as f:
                    return json.load(f)
            except:
                return []
        return []
    
    def _save_index(self, index: List[Dict]):
        """Save sticker index"""
        with open(self.index_path, 'w') as f:
            json.dump(index, f, indent=2)
    
    def get_response_recommendation(self, received_sticker_analysis: Dict) -> Dict:
        """
        Recommend how to respond to a received sticker
        
        Returns:
            - response_type: 'match', 'roast', 'escalate', 'de-escalate', 'react_only'
            - suggested_vibes: list of vibe keywords to search
            - suggested_emotion: emotion to match
            - should_include_text: whether to add text with sticker
            - text_template: suggested text (if any)
        """
        
        emotion = received_sticker_analysis.get('emotion', 'neutral')
        vulgarity = received_sticker_analysis.get('vulgarity', 'none')
        humor_level = received_sticker_analysis.get('humor_level', 'none')
        intensity = received_sticker_analysis.get('intensity', 'medium')
        
        recommendation = {
            "response_type": "match",
            "suggested_vibes": [],
            "suggested_emotion": emotion,
            "include_vulgar": vulgarity != 'none',
            "max_vulgarity": vulgarity,
            "should_include_text": False,
            "text_template": None
        }
        
        # Humor response
        if humor_level in ['medium', 'high']:
            recommendation["response_type"] = "match"
            recommendation["suggested_vibes"] = ["laughing", "funny", "roasting"]
            recommendation["suggested_emotion"] = "laughing"
        
        # Vulgar/roasting response
        if vulgarity in ['moderate', 'high']:
            recommendation["response_type"] = "roast"
            recommendation["suggested_vibes"] = ["roasting", "angry", "middle_finger"]
            recommendation["include_vulgar"] = True
            recommendation["max_vulgarity"] = vulgarity
        
        # Emotional response
        if emotion in ['sad', 'crying']:
            recommendation["response_type"] = "comfort"
            recommendation["suggested_vibes"] = ["supportive", "hug", "care"]
            recommendation["should_include_text"] = True
            recommendation["text_template"] = "Kya hua yaar?"
        
        elif emotion in ['angry', 'frustrated']:
            recommendation["response_type"] = "de-escalate"
            recommendation["suggested_vibes"] = ["calm", "peace", "sorry"]
            recommendation["should_include_text"] = True
            recommendation["text_template"] = "Chill bro"
        
        elif emotion in ['love', 'romantic']:
            recommendation["response_type"] = "match"
            recommendation["suggested_vibes"] = ["love", "heart", "care"]
        
        # High intensity needs strong response
        if intensity == 'high':
            recommendation["suggested_vibes"] = [v + " intense" for v in recommendation["suggested_vibes"]]
        
        return recommendation