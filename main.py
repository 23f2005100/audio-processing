import os
import base64
import tempfile
import subprocess
import httpx
import json
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

# ── Config ────────────────────────────────────────────────
AIPIPE_TOKEN = os.getenv("AIPIPE_TOKEN")

# Use OpenRouter via aipipe — OpenAI-compatible, supports Gemini
OPENROUTER_BASE = "https://aipipe.org/openrouter/v1"
MODEL = "google/gemini-2.0-flash-001"  # Gemini via OpenRouter

HEADERS = {
    "Authorization": f"Bearer {AIPIPE_TOKEN}",
    "Content-Type": "application/json",
}

# ── Request / Response models ─────────────────────────────
class AskRequest(BaseModel):
    video_url: str
    topic: str

class AskResponse(BaseModel):
    timestamp: str
    video_url: str
    topic: str


# ── Main endpoint ─────────────────────────────────────────
@app.post("/ask", response_model=AskResponse)
async def ask(request: AskRequest):
    audio_base = None
    audio_path = None

    try:
        # ── STEP 1: Download audio from YouTube ──────────
        with tempfile.NamedTemporaryFile(suffix="", delete=False, prefix="yt_audio_") as tmp:
            audio_base = tmp.name

        audio_path = audio_base + ".mp3"

        ydl_command = [
            "yt-dlp",
            "--extract-audio",
            "--audio-format", "mp3",
            "--audio-quality", "5",
            "-o", audio_base + ".%(ext)s",
            "--no-playlist",
            request.video_url,
        ]

        result = subprocess.run(ydl_command, capture_output=True, text=True, timeout=120)

        if not os.path.exists(audio_path):
            raise HTTPException(status_code=400, detail=f"yt-dlp failed: {result.stderr}")

        print(f"Downloaded: {audio_path} ({os.path.getsize(audio_path)} bytes)")

        # ── STEP 2: Read audio and encode as base64 ───────
        # OpenRouter/Gemini accepts audio as base64 inline
        # No Files API needed — much simpler!
        with open(audio_path, "rb") as f:
            audio_b64 = base64.b64encode(f.read()).decode("utf-8")

        print(f"Encoded audio as base64 ({len(audio_b64)} chars)")

        # ── STEP 3: Ask Gemini via aipipe OpenRouter ──────
        prompt = f"""Listen carefully to this audio.

Find the FIRST moment when the topic "{request.topic}" is spoken or discussed.

Reply ONLY with a JSON object like this:
{{"timestamp": "00:05:47"}}

Rules:
- Format must be HH:MM:SS (always include hours, e.g. "00:05:47" not "5:47")
- Return only the first occurrence
- If it starts at the very beginning, return "00:00:00"
- No explanation, just the JSON"""

        payload = {
            "model": MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": prompt
                        },
                        {
                            "type": "image_url",  # OpenRouter uses image_url for audio too
                            "image_url": {
                                "url": f"data:audio/mp3;base64,{audio_b64}"
                            }
                        }
                    ]
                }
            ],
            "response_format": {"type": "json_object"},  # Force JSON response
        }

        with httpx.Client(timeout=180) as client:
            response = client.post(
                f"{OPENROUTER_BASE}/chat/completions",
                headers=HEADERS,
                json=payload
            )
            response.raise_for_status()
            data = response.json()

        # ── STEP 4: Parse the response ─────────────────────
        raw_text = data["choices"][0]["message"]["content"].strip()
        print(f"Gemini response: {raw_text}")

        # Clean up markdown fences if present
        clean_text = raw_text.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(clean_text)
        timestamp = parsed.get("timestamp", "00:00:00")

        # Ensure HH:MM:SS format
        parts = timestamp.split(":")
        if len(parts) == 2:
            timestamp = f"00:{timestamp}"
        elif len(parts) == 1:
            secs = int(timestamp)
            h, remainder = divmod(secs, 3600)
            m, s = divmod(remainder, 60)
            timestamp = f"{h:02d}:{m:02d}:{s:02d}"

        return AskResponse(
            timestamp=timestamp,
            video_url=request.video_url,
            topic=request.topic,
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    finally:
        # ── CLEANUP ───────────────────────────────────────
        for path in [audio_path, audio_base]:
            if path and os.path.exists(path):
                os.unlink(path)
                print(f"Deleted: {path}")