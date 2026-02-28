"""
Async client for the MusicGPT API (api.musicgpt.com).

Endpoints used
--------------
POST /v1/inpaint
    Upload a song and replace a specific time window with AI-generated content
    guided by a text prompt. This is the core "seamless integration" endpoint.
    Accepts audio_file (multipart) directly -- no external hosting step required.

POST /v1/MusicAI
    Generate a brand-new music track from a text prompt + optional style/lyrics.
    Used when ad_mode=text_prompt and seamless_integration=False.

GET /v1/byId
    Poll a task or conversion ID until status is COMPLETED or FAILED.
    Returns the final audio_url.

Authentication: Authorization header with raw API key (no "Bearer" prefix).
"""

import asyncio
import logging
from pathlib import Path
from typing import Optional

import httpx

from .config import settings

logger = logging.getLogger(__name__)


class MusicGPTError(Exception):
    """Raised when the MusicGPT API returns an error or times out."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


def _auth_headers() -> dict[str, str]:
    if not settings.musicgpt_api_key:
        raise MusicGPTError("MUSICGPT_API_KEY is not configured.")
    return {"Authorization": settings.musicgpt_api_key}


def _pick_audio_url(value: object) -> str:
    """Best-effort extraction of an audio URL from nested provider payloads."""
    if isinstance(value, str):
        if value.startswith("http://") or value.startswith("https://"):
            return value
        return ""
    if isinstance(value, dict):
        # Prefer explicit keys first.
        for key in ("audio_url", "audioUrl", "conversion_path", "file_url", "url"):
            v = value.get(key)
            if isinstance(v, str) and (v.startswith("http://") or v.startswith("https://")):
                return v
        # Fall back to deep scan.
        for nested in value.values():
            picked = _pick_audio_url(nested)
            if picked:
                return picked
        return ""
    if isinstance(value, list):
        for item in value:
            picked = _pick_audio_url(item)
            if picked:
                return picked
        return ""
    return ""


# ---------------------------------------------------------------------------
# Inpaint  (seamless segment replacement inside an existing song)
# ---------------------------------------------------------------------------


async def inpaint_song(
    audio_path: Path,
    prompt: str,
    replace_start_at: float,
    replace_end_at: float,
    lyrics: Optional[str] = None,
    lyrics_section_to_replace: Optional[str] = None,
    gender: Optional[str] = None,
) -> tuple[str, str]:
    """
    Submit an inpaint request to replace a segment of a song.

    The song is uploaded directly as multipart/form-data -- no external URL needed.

    Parameters
    ----------
    audio_path : Path
        Local path to the source song.
    prompt : str
        Description of how the replaced segment should sound.
        Example: "A catchy 15-second upbeat pop advertisement jingle."
    replace_start_at : float
        Start time in seconds of the segment to replace.
    replace_end_at : float
        End time in seconds of the segment to replace.
    lyrics : str, optional
        Full lyrics of the original song (improves quality).
    lyrics_section_to_replace : str, optional
        Lyrics for the replacement section (max 3000 chars).
    gender : str, optional
        Voice style: "male", "female", or "neutral".

    Returns
    -------
    tuple[task_id, conversion_id_1]
        Use task_id + "INPAINT" with poll_task() to wait for completion.
        conversion_id_1 is the primary output version.
    """
    data: dict[str, str] = {
        "prompt": prompt,
        "replace_start_at": str(replace_start_at),
        "replace_end_at": str(replace_end_at),
    }
    if lyrics:
        data["lyrics"] = lyrics[:3000]
    if lyrics_section_to_replace:
        data["lyrics_section_to_replace"] = lyrics_section_to_replace[:3000]
    if gender and gender in ("male", "female", "neutral"):
        data["gender"] = gender

    async with httpx.AsyncClient(timeout=120) as client:
        with audio_path.open("rb") as fh:
            resp = await client.post(
                f"{settings.musicgpt_api_base}/v1/inpaint",
                headers=_auth_headers(),
                data=data,
                files={"audio_file": (audio_path.name, fh, "audio/mpeg")},
            )

    if not resp.is_success:
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        raise MusicGPTError(
            f"Inpaint request failed (HTTP {resp.status_code}): {body.get('error') or resp.text[:300]}",
            status_code=resp.status_code,
        )

    body = resp.json()
    if not body.get("success"):
        raise MusicGPTError(f"Inpaint rejected: {body.get('error') or body}")

    task_id: str = body["task_id"]
    conversion_id: str = body.get("conversion_id_1") or body.get("conversion_id_2") or ""
    logger.info("Inpaint submitted task_id=%s conversion_id=%s eta=%ss", task_id, conversion_id, body.get("eta"))
    return task_id, conversion_id


# ---------------------------------------------------------------------------
# Music generation  (text-prompt ad creation)
# ---------------------------------------------------------------------------


async def generate_music(
    prompt: str,
    music_style: Optional[str] = None,
    lyrics: Optional[str] = None,
    make_instrumental: bool = False,
    gender: Optional[str] = None,
    output_length: Optional[float] = None,
) -> tuple[str, str]:
    """
    Generate a new music track from a text prompt.

    Parameters
    ----------
    prompt : str
        Natural language description of the music. Keep under 280 chars.
    music_style : str, optional
        Genre/style e.g. "Pop", "Jazz", "Electronic".
    lyrics : str, optional
        Custom lyrics to use.
    make_instrumental : bool
        If True, generate without vocals.
    gender : str, optional
        Vocal style: "male", "female", or "neutral".
    output_length : float, optional
        Target output duration in seconds (experimental).

    Returns
    -------
    tuple[task_id, conversion_id_1]
        Use task_id + "MUSIC_AI" with poll_task() to wait for completion.
    """
    payload: dict = {"prompt": prompt}
    if music_style:
        payload["music_style"] = music_style
    if lyrics:
        payload["lyrics"] = lyrics
    if make_instrumental:
        payload["make_instrumental"] = True
    if gender and gender in ("male", "female", "neutral"):
        payload["gender"] = gender
    if output_length is not None:
        payload["output_length"] = output_length

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{settings.musicgpt_api_base}/v1/MusicAI",
            headers={**_auth_headers(), "Content-Type": "application/json"},
            json=payload,
        )

    if not resp.is_success:
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        raise MusicGPTError(
            f"Music generation failed (HTTP {resp.status_code}): {body.get('error') or resp.text[:300]}",
            status_code=resp.status_code,
        )

    body = resp.json()
    if not body.get("success"):
        raise MusicGPTError(f"Music generation rejected: {body.get('error') or body}")

    task_id: str = body["task_id"]
    conversion_id: str = body.get("conversion_id_1") or body.get("conversion_id_2") or ""
    logger.info("MusicAI submitted task_id=%s conversion_id=%s eta=%ss", task_id, conversion_id, body.get("eta"))
    return task_id, conversion_id


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------


async def poll_task(
    task_id: str,
    conversion_type: str,
    conversion_id: Optional[str] = None,
) -> str:
    """
    Poll GET /v1/byId until the task is COMPLETED, then return the audio URL.

    Parameters
    ----------
    task_id : str
        The task_id returned by inpaint_song() or generate_music().
    conversion_type : str
        MusicGPT conversion type string e.g. "INPAINT" or "MUSIC_AI".

    Returns
    -------
    str
        Direct URL to the completed audio file.

    Raises
    ------
    MusicGPTError
        On FAILED status, timeout, or missing audio URL.
    """
    deadline = asyncio.get_event_loop().time() + settings.poll_timeout_seconds
    consecutive_server_errors = 0

    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            # Try task_id first; if conversion_id is available we keep it as a fallback.
            poll_param_sets = [
                {"conversionType": conversion_type, "task_id": task_id},
            ]
            if conversion_id:
                poll_param_sets.append(
                    {"conversionType": conversion_type, "conversion_id": conversion_id}
                )

            last_error: Optional[MusicGPTError] = None
            resp: Optional[httpx.Response] = None
            used_params: Optional[dict[str, str]] = None
            for params in poll_param_sets:
                resp = await client.get(
                    f"{settings.musicgpt_api_base}/v1/byId",
                    headers=_auth_headers(),
                    params=params,
                )
                if resp.is_success:
                    used_params = params
                    break
                # Many providers can return intermittent 5xx while conversion is still warming up.
                if 500 <= resp.status_code < 600:
                    last_error = MusicGPTError(
                        f"Poll request failed (HTTP {resp.status_code}): {resp.text[:300]}",
                        status_code=resp.status_code,
                    )
                    continue
                raise MusicGPTError(
                    f"Poll request failed (HTTP {resp.status_code}): {resp.text[:300]}",
                    status_code=resp.status_code,
                )

            if resp is None:
                raise MusicGPTError("Polling failed before an HTTP response was received.")

            if not resp.is_success:
                consecutive_server_errors += 1
                if consecutive_server_errors <= 10:
                    logger.warning(
                        "Task %s poll got transient server error (%s/%s): %s",
                        task_id,
                        consecutive_server_errors,
                        10,
                        last_error or resp.text[:150],
                    )
                    await asyncio.sleep(min(settings.poll_interval_seconds * 2, 10.0))
                    continue
                raise MusicGPTError(
                    f"Poll request repeatedly failed with server errors: {last_error or resp.text[:300]}",
                    status_code=resp.status_code,
                )

            consecutive_server_errors = 0

            body = resp.json()
            if not body.get("success"):
                error_msg = body.get("message") or body.get("error") or body
                raise MusicGPTError(f"Poll error: {error_msg}")

            conversion = body.get("conversion") or {}
            status = str(conversion.get("status") or "").upper()

            if status == "COMPLETED":
                audio_url = _pick_audio_url(conversion) or _pick_audio_url(body)
                # Some responses queried by task_id may not carry the final file URL.
                # Try one direct conversion_id lookup before failing.
                if (
                    not audio_url
                    and conversion_id
                    and used_params
                    and "task_id" in used_params
                ):
                    retry_resp = await client.get(
                        f"{settings.musicgpt_api_base}/v1/byId",
                        headers=_auth_headers(),
                        params={"conversionType": conversion_type, "conversion_id": conversion_id},
                    )
                    if retry_resp.is_success:
                        retry_body = retry_resp.json()
                        retry_conv = retry_body.get("conversion") or {}
                        audio_url = _pick_audio_url(retry_conv) or _pick_audio_url(retry_body)
                if not audio_url:
                    raise MusicGPTError(
                        f"Task {task_id} completed but returned no audio URL. "
                        f"Provider payload keys={list(conversion.keys())}"
                    )
                logger.info("Task %s COMPLETED audio_url=%s", task_id, audio_url)
                return audio_url

            if status in ("FAILED", "ERROR"):
                msg = conversion.get("status_msg") or "No details"
                raise MusicGPTError(f"Task {task_id} failed: {msg}")

            if asyncio.get_event_loop().time() > deadline:
                raise MusicGPTError(f"Task {task_id} timed out after {settings.poll_timeout_seconds}s")

            logger.info("Task %s status=%s, waiting...", task_id, status or "IN_QUEUE")
            await asyncio.sleep(settings.poll_interval_seconds)


# ---------------------------------------------------------------------------
# Download helper
# ---------------------------------------------------------------------------


async def download_audio(url: str, dest: Path) -> Path:
    """Download a remote audio URL to *dest* and return the path."""
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
    logger.info("Downloaded %s -> %s (%d bytes)", url, dest, len(resp.content))
    return dest
