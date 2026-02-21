import asyncio
import os
from dotenv import load_dotenv

# Load env file to get SARVAM_API_KEY
load_dotenv("backend/.env")

from src.core.media_processor import MediaProcessor

async def test_sarvam_audio():
    print("Testing MediaProcessor with Sarvam STT...")
    processor = MediaProcessor(openai_client=None, sticker_analyzer=None)
    
    # Send the test file
    res = await processor.process("test_voice.mp3", "audio")
    print(f"\nFinal Expected Result Check:")
    print(f"============================")
    print(res)
    print(f"============================\n")

if __name__ == "__main__":
    asyncio.run(test_sarvam_audio())
