"""
Ad Injector – FastAPI application.

Endpoints
---------
POST /upload/song          – upload a source song, get a file_id back
POST /upload/ad            – upload an ad audio clip, get a file_id back
POST /inject               – start an ad-injection job (background task)
GET  /jobs/{job_id}        – poll job status
GET  /download/{filename}  – download finished output
GET  /                     – serve the single-page frontend
"""

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Optional

import aiofiles
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .audio_processor import get_duration, inject_ad
from .config import settings
from .musicgpt_client import MusicGPTError, download_audio, generate_music, inpaint_song, poll_task
from .schemas import AdMode, InjectRequest, JobResponse, JobStatus, UploadResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Ad Injector", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static files (frontend)
_static_dir = Path(__file__).parent / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

# In-memory job store
jobs: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_file(file_id: str, subdir: str) -> Path:
    """Find a previously uploaded file by its file_id stem."""
    base = settings.upload_dir / subdir
    matches = list(base.glob(f"{file_id}.*"))
    if not matches:
        raise HTTPException(status_code=404, detail=f"File not found: {file_id}")
    return matches[0]


async def _save_upload(upload: UploadFile, subdir: str) -> tuple[str, Path]:
    """Save an uploaded file and return (file_id, path)."""
    file_id = uuid.uuid4().hex
    suffix = Path(upload.filename or "audio.mp3").suffix or ".mp3"
    dest_dir = settings.upload_dir / subdir
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{file_id}{suffix}"
    async with aiofiles.open(dest, "wb") as fh:
        content = await upload.read()
        await fh.write(content)
    return file_id, dest


# ---------------------------------------------------------------------------
# Background job
# ---------------------------------------------------------------------------


async def _run_injection_job(job_id: str, req: InjectRequest) -> None:
    try:
        song_path = _resolve_file(req.song_id, "songs")
        output_filename = f"{job_id}_output.mp3"
        output_path = settings.output_dir / output_filename

        ad_path: Optional[Path] = None
        if req.ad_mode == AdMode.audio_upload:
            if not req.ad_id:
                raise RuntimeError("ad_id is required when ad_mode=audio_upload")
            ad_path = _resolve_file(req.ad_id, "ads")

        # ------------------------------------------------------------------
        # Path A: Seamless integration (MusicGPT Inpaint)
        # ------------------------------------------------------------------
        if req.seamless_integration:
            jobs[job_id]["message"] = "Submitting inpaint job to MusicGPT..."
            logger.info("[%s] Seamless mode: starting inpaint", job_id)

            replace_length = req.ad_length_seconds if (req.ad_length_seconds and req.ad_length_seconds > 0) else req.replace_window_seconds
            replace_length = max(1.0, replace_length)
            replace_start = req.insert_at_seconds
            replace_end = req.insert_at_seconds + replace_length

            # Derive the prompt
            if req.ad_integration_prompt:
                prompt = req.ad_integration_prompt
            elif req.ad_mode == AdMode.text_prompt and req.ad_text_prompt:
                prompt = req.ad_text_prompt
            elif ad_path is not None:
                prompt = f"A short advertisement segment blending with the surrounding music."
            else:
                prompt = "A catchy advertisement jingle that blends seamlessly with the surrounding music."

            task_id, conversion_id = await inpaint_song(
                audio_path=song_path,
                prompt=prompt,
                replace_start_at=replace_start,
                replace_end_at=replace_end,
                gender=req.gender,
            )

            jobs[job_id]["message"] = "Waiting for MusicGPT inpaint to complete..."
            audio_url = await poll_task(task_id, "INPAINT", conversion_id=conversion_id or None)

            jobs[job_id]["message"] = "Downloading result..."
            await download_audio(audio_url, output_path)
            duration = get_duration(output_path)

        # ------------------------------------------------------------------
        # Path B: Local splice with optional MusicGPT generation
        # ------------------------------------------------------------------
        else:
            effective_ad_path: Path

            if req.ad_mode == AdMode.text_prompt:
                if not (req.ad_text_prompt or "").strip():
                    raise RuntimeError("ad_text_prompt is required when ad_mode=text_prompt")

                jobs[job_id]["message"] = "Generating ad audio with MusicGPT..."
                task_id, conversion_id = await generate_music(
                    prompt=(req.ad_text_prompt or "").strip(),
                    music_style=req.music_style,
                    gender=req.gender,
                    output_length=req.ad_length_seconds,
                )
                jobs[job_id]["message"] = "Waiting for MusicGPT music generation..."
                audio_url = await poll_task(task_id, "MUSIC_AI", conversion_id=conversion_id or None)

                generated_ad_dir = settings.upload_dir / "ads"
                generated_ad_dir.mkdir(parents=True, exist_ok=True)
                generated_ad_path = generated_ad_dir / f"{job_id}_generated_ad.mp3"
                await download_audio(audio_url, generated_ad_path)
                effective_ad_path = generated_ad_path

            else:
                if ad_path is None:
                    raise RuntimeError("ad_id is required when ad_mode=audio_upload")
                effective_ad_path = ad_path

            jobs[job_id]["message"] = "Mixing audio locally..."
            duration = await inject_ad(
                song_path=song_path,
                ad_path=effective_ad_path,
                output_path=output_path,
                insert_at_seconds=req.insert_at_seconds,
                crossfade_ms=req.crossfade_ms,
                duck_volume_db=req.duck_volume_db,
                ad_length_seconds=req.ad_length_seconds,
                instrumental_path=None,
            )

        jobs[job_id].update(
            {
                "status": JobStatus.complete,
                "message": "Done",
                "output_filename": output_filename,
                "duration_seconds": duration,
            }
        )
        logger.info("[%s] Job complete: %s (%.1fs)", job_id, output_filename, duration)

    except MusicGPTError as exc:
        logger.error("[%s] MusicGPT error: %s", job_id, exc)
        jobs[job_id].update({"status": JobStatus.failed, "error": str(exc)})
    except HTTPException as exc:
        logger.error("[%s] HTTP error: %s", job_id, exc.detail)
        jobs[job_id].update({"status": JobStatus.failed, "error": exc.detail})
    except Exception as exc:
        logger.exception("[%s] Unexpected error", job_id)
        jobs[job_id].update({"status": JobStatus.failed, "error": str(exc)})


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", include_in_schema=False)
async def serve_frontend():
    index = _static_dir / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return {"message": "Ad Injector API is running. See /docs for the OpenAPI spec."}


@app.post("/upload/song", response_model=UploadResponse, summary="Upload source song")
async def upload_song(file: UploadFile = File(...)):
    file_id, path = await _save_upload(file, "songs")
    try:
        duration = get_duration(path)
    except Exception:
        duration = None
    return UploadResponse(
        file_id=file_id,
        filename=file.filename or path.name,
        size_bytes=path.stat().st_size,
        duration_seconds=duration,
    )


@app.post("/upload/ad", response_model=UploadResponse, summary="Upload ad audio clip")
async def upload_ad(file: UploadFile = File(...)):
    file_id, path = await _save_upload(file, "ads")
    try:
        duration = get_duration(path)
    except Exception:
        duration = None
    return UploadResponse(
        file_id=file_id,
        filename=file.filename or path.name,
        size_bytes=path.stat().st_size,
        duration_seconds=duration,
    )


@app.post("/inject", response_model=JobResponse, summary="Start an ad injection job")
async def inject(req: InjectRequest, background_tasks: BackgroundTasks):
    # Validate required fields based on mode
    if req.ad_mode == AdMode.audio_upload and not req.ad_id:
        raise HTTPException(status_code=422, detail="ad_id is required when ad_mode=audio_upload")
    if req.ad_mode == AdMode.text_prompt and not (req.ad_text_prompt or "").strip():
        raise HTTPException(
            status_code=422, detail="ad_text_prompt is required when ad_mode=text_prompt"
        )

    job_id = uuid.uuid4().hex
    jobs[job_id] = {
        "status": JobStatus.processing,
        "message": "Job queued",
        "output_filename": None,
        "duration_seconds": None,
        "error": None,
    }

    background_tasks.add_task(_run_injection_job, job_id, req)
    return JobResponse(job_id=job_id, status=JobStatus.processing, message="Job queued")


@app.get("/jobs/{job_id}", response_model=JobResponse, summary="Poll job status")
async def get_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return JobResponse(job_id=job_id, **job)


@app.get("/download/{filename}", summary="Download a finished output file")
async def download_result(filename: str):
    path = settings.output_dir / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Output file not found")
    return FileResponse(str(path), media_type="audio/mpeg", filename=filename)


@app.get("/health", summary="Health check")
async def health():
    return {"status": "ok"}
