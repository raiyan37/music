from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    pending = "pending"
    processing = "processing"
    complete = "complete"
    failed = "failed"


class AdMode(str, Enum):
    audio_upload = "audio_upload"
    text_prompt = "text_prompt"


class UploadResponse(BaseModel):
    file_id: str
    filename: str
    size_bytes: int
    duration_seconds: Optional[float] = None


class InjectRequest(BaseModel):
    song_id: str = Field(..., description="File ID returned by POST /upload/song")
    ad_mode: AdMode = Field(
        AdMode.audio_upload,
        description="How ad content is provided: uploaded audio file or a text prompt.",
    )
    ad_id: Optional[str] = Field(
        None,
        description="File ID returned by POST /upload/ad. Required when ad_mode=audio_upload.",
    )
    ad_text_prompt: Optional[str] = Field(
        None,
        max_length=1000,
        description=(
            "Text description of the ad to generate. "
            "Required when ad_mode=text_prompt. "
            "Example: 'A catchy 15-second upbeat coffee app jingle.'"
        ),
    )
    insert_at_seconds: float = Field(
        ..., gt=0, description="Position in the song (seconds) where the ad starts."
    )
    seamless_integration: bool = Field(
        True,
        description=(
            "When true, use MusicGPT Inpaint to replace the target window of the song "
            "with AI-generated ad content that blends with the surrounding music. "
            "When false, splice the ad audio in using local crossfade/ducking."
        ),
    )
    replace_window_seconds: float = Field(
        15.0,
        ge=1.0,
        le=120.0,
        description=(
            "Length of the window (seconds) to replace in seamless mode. "
            "The window runs from insert_at_seconds to insert_at_seconds + replace_window_seconds."
        ),
    )
    ad_integration_prompt: Optional[str] = Field(
        None,
        max_length=1000,
        description=(
            "Prompt describing how the replaced section should sound in seamless mode. "
            "Example: 'A punchy 15-second advertisement for a coffee app with an upbeat feel.' "
            "If omitted, a default prompt is derived from ad_text_prompt or the uploaded ad filename."
        ),
    )
    music_style: Optional[str] = Field(
        None,
        max_length=200,
        description=(
            "Music genre/style for MusicGPT generation, e.g. 'Pop', 'Jazz', 'Electronic'. "
            "Used when ad_mode=text_prompt and seamless_integration=False."
        ),
    )
    gender: Optional[str] = Field(
        None,
        description="Preferred vocal gender for MusicGPT: 'male', 'female', or 'neutral'.",
    )
    crossfade_ms: int = Field(
        500, ge=0, le=5000, description="Crossfade duration in ms at each splice point (local mode only)."
    )
    duck_volume_db: float = Field(
        -8.0, le=0, description="dB to lower song volume during the ad window (local mode, 0 = off)."
    )


class JobResponse(BaseModel):
    job_id: str
    status: JobStatus
    message: Optional[str] = None
    output_filename: Optional[str] = None
    duration_seconds: Optional[float] = None
    error: Optional[str] = None
