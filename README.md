# Ad Injector

Seamlessly insert an advertisement into any song using **MusicGPT AI**.

- **Seamless mode** – MusicGPT's Inpaint endpoint replaces a chosen time window in the song with AI-generated ad content that musically blends with the surrounding audio.
- **Local splice mode** – Classic pydub crossfade/volume-ducking splice. Supports both uploaded ad audio and AI-generated ads from a text prompt.

---

## Setup

### 1. Get a MusicGPT API key

1. Sign up at [musicgpt.com](https://musicgpt.com) — new accounts get **$20 free credits**.
2. Go to your dashboard / API settings and copy the key.

### 2. Install system dependencies

```bash
# macOS
brew install ffmpeg

# Ubuntu / Debian
sudo apt-get install ffmpeg
```

### 3. Install Python dependencies

```bash
cd "ad-injector"
pip install -r requirements.txt
```

### 4. Configure environment

```bash
cp .env.example .env
# then edit .env and paste your key:
#   MUSICGPT_API_KEY=<your_key>
```

### 5. Run the server

```bash
uvicorn app.main:app --reload
```

Open **http://127.0.0.1:8000** in your browser to use the web UI.

---

## API reference

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/upload/song` | Upload source song → returns `file_id` |
| `POST` | `/upload/ad` | Upload ad audio → returns `file_id` |
| `POST` | `/inject` | Start injection job → returns `job_id` |
| `GET` | `/jobs/{job_id}` | Poll job status |
| `GET` | `/download/{filename}` | Download finished MP3 |

Interactive docs: **http://127.0.0.1:8000/docs**

### `POST /inject` payload

```json
{
  "song_id": "<file_id from /upload/song>",
  "ad_mode": "audio_upload",
  "ad_id": "<file_id from /upload/ad>",
  "insert_at_seconds": 30,
  "seamless_integration": true,
  "replace_window_seconds": 15,
  "ad_integration_prompt": "A punchy 15-second coffee-app ad, upbeat pop",
  "gender": "female"
}
```

**Ad modes**

| `ad_mode` | Required field | Description |
|-----------|---------------|-------------|
| `audio_upload` | `ad_id` | Use a pre-recorded ad clip |
| `text_prompt` | `ad_text_prompt` | MusicGPT generates the ad from your description |

**Key fields**

| Field | Default | Description |
|-------|---------|-------------|
| `seamless_integration` | `true` | Use MusicGPT Inpaint vs. local pydub splice |
| `replace_window_seconds` | `15` | Length of the replaced window (seamless mode) |
| `ad_integration_prompt` | — | How the ad should sound in seamless mode |
| `music_style` | — | Genre hint for AI generation e.g. `"Pop"` |
| `gender` | — | Vocal style: `male` / `female` / `neutral` |
| `crossfade_ms` | `500` | Crossfade length for local splice |
| `duck_volume_db` | `-8` | Song volume reduction during ad (local mode) |

---

## CLI testbench

```bash
# Seamless integration with a text-prompt ad:
python testbench.py song.mp3 \
    --text-prompt "A catchy 15-second upbeat jingle for a coffee delivery app" \
    --insert-at 30 --window 15

# Seamless integration with uploaded ad audio:
python testbench.py song.mp3 --ad ad.mp3 \
    --insert-at 45 --window 12 \
    --prompt "A punchy advertisement blending with the surrounding pop music"

# Local splice with uploaded ad:
python testbench.py song.mp3 --ad ad.mp3 --insert-at 60 --no-seamless

# Local splice with AI-generated ad:
python testbench.py song.mp3 \
    --text-prompt "An energetic sneaker brand ad" \
    --insert-at 30 --no-seamless --music-style Electronic
```

Run `python testbench.py --help` for all options.

---

## How it works

```
User uploads song + (ad file or text prompt)
        │
        ▼
seamless_integration?
    ├── YES → POST /v1/inpaint (MusicGPT)
    │          audio_file = song
    │          replace_start_at / replace_end_at
    │          prompt = ad description
    │          → poll GET /v1/byId → download final MP3
    │
    └── NO  → ad_mode?
              ├── text_prompt → POST /v1/MusicAI → poll → download ad
              │                 then pydub crossfade + duck
              └── audio_upload → pydub crossfade + duck directly
```
