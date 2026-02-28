#!/usr/bin/env python3
"""
Ad Injector – CLI testbench

Usage examples
--------------
# Seamless integration (MusicGPT Inpaint replaces a window in the song):
python testbench.py song.mp3 --seamless --insert-at 30 --window 15 \
    --prompt "A catchy 15-second coffee-app ad, upbeat pop"

# Seamless + uploaded ad audio (prompt derived from filename if not given):
python testbench.py song.mp3 --seamless --insert-at 30 --window 15 \
    --ad ad.mp3 \
    --prompt "A punchy coffee advertisement blending with the surrounding music"

# Local splice with uploaded ad (pydub crossfade):
python testbench.py song.mp3 --ad ad.mp3 --insert-at 45

# Local splice with AI-generated ad (text prompt → MusicGPT):
python testbench.py song.mp3 --text-prompt "A 15-second upbeat pop jingle for a shoe brand" \
    --insert-at 60 --music-style Pop

# Specify output file explicitly:
python testbench.py song.mp3 --ad ad.mp3 --insert-at 30 --out result.mp3
"""

import argparse
import sys
import time
import urllib.request
import urllib.error
import json
import os

BASE_URL = os.environ.get("AD_INJECTOR_URL", "http://127.0.0.1:8000")


def api(method: str, path: str, **kwargs):
    url = BASE_URL + path
    data = kwargs.get("json")
    files = kwargs.get("files")
    headers = kwargs.get("headers", {})

    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
    elif files is not None:
        boundary = "----AdInjectorBoundary"
        parts = []
        for field_name, file_tuple in files.items():
            fname, fobj, mime = file_tuple
            content = fobj.read()
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{field_name}"; '
                f'filename="{fname}"\r\nContent-Type: {mime}\r\n\r\n'.encode() + content + b"\r\n"
            )
        body = b"".join(parts) + f"--{boundary}--\r\n".encode()
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        req = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
    else:
        req = urllib.request.Request(url, headers=headers, method=method.upper())

    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"HTTP {e.code}: {body}", file=sys.stderr)
        sys.exit(1)


def upload_file(path: str, endpoint: str) -> str:
    print(f"  Uploading {path} …", end=" ", flush=True)
    with open(path, "rb") as fh:
        fname = os.path.basename(path)
        resp = api("POST", endpoint, files={"file": (fname, fh, "audio/mpeg")})
    file_id = resp["file_id"]
    dur = resp.get("duration_seconds")
    dur_str = f" ({dur:.1f}s)" if dur else ""
    print(f"OK → {file_id}{dur_str}")
    return file_id


def main():
    parser = argparse.ArgumentParser(
        description="CLI testbench for the Ad Injector backend.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("song", help="Path to the source song file")
    parser.add_argument("--ad", help="Path to an ad audio file (audio_upload mode)")
    parser.add_argument("--text-prompt", dest="text_prompt", help="Text prompt to generate ad (text_prompt mode)")
    parser.add_argument("--insert-at", dest="insert_at", type=float, default=30.0,
                        help="Seconds into the song where the ad starts (default: 30)")
    parser.add_argument("--seamless", action="store_true", default=True,
                        help="Use MusicGPT Inpaint for seamless integration (default: True)")
    parser.add_argument("--no-seamless", dest="seamless", action="store_false",
                        help="Use local pydub splice instead of Inpaint")
    parser.add_argument("--window", type=float, default=30.0,
                        help="Replace window in seconds for seamless mode (default: 30)")
    parser.add_argument("--prompt", dest="integration_prompt",
                        help="Integration/inpaint prompt for seamless mode")
    parser.add_argument("--music-style", dest="music_style",
                        help="Music style for AI generation e.g. 'Pop', 'Jazz'")
    parser.add_argument("--gender", choices=["male", "female", "neutral"],
                        help="Vocal gender preference for MusicGPT")
    parser.add_argument("--crossfade", type=int, default=500,
                        help="Crossfade ms for local splice (default: 500)")
    parser.add_argument("--duck-db", dest="duck_db", type=float, default=-8.0,
                        help="Volume ducking dB for local splice (default: -8)")
    parser.add_argument("--out", default="output.mp3", help="Output filename (default: output.mp3)")
    parser.add_argument("--url", default=None, help=f"Backend URL (default: {BASE_URL})")
    args = parser.parse_args()

    global BASE_URL
    if args.url:
        BASE_URL = args.url.rstrip("/")

    print(f"\nAd Injector testbench  →  {BASE_URL}\n")

    # Upload song
    song_id = upload_file(args.song, "/upload/song")

    # Determine ad mode and upload/validate
    if args.ad:
        ad_mode = "audio_upload"
        ad_id = upload_file(args.ad, "/upload/ad")
        ad_text_prompt = None
    elif args.text_prompt:
        ad_mode = "text_prompt"
        ad_id = None
        ad_text_prompt = args.text_prompt
    else:
        print("Error: provide --ad <file> or --text-prompt <prompt>", file=sys.stderr)
        sys.exit(1)

    # Build inject request
    payload: dict = {
        "song_id": song_id,
        "ad_mode": ad_mode,
        "insert_at_seconds": args.insert_at,
        "seamless_integration": args.seamless,
        "replace_window_seconds": args.window,
        "crossfade_ms": args.crossfade,
        "duck_volume_db": args.duck_db,
    }
    if ad_id:
        payload["ad_id"] = ad_id
    if ad_text_prompt:
        payload["ad_text_prompt"] = ad_text_prompt
    if args.integration_prompt:
        payload["ad_integration_prompt"] = args.integration_prompt
    if args.music_style:
        payload["music_style"] = args.music_style
    if args.gender:
        payload["gender"] = args.gender

    # Submit job
    print(f"\n  Submitting inject job …", end=" ", flush=True)
    resp = api("POST", "/inject", json=payload)
    job_id = resp["job_id"]
    print(f"OK → job_id={job_id}")

    # Poll
    print("  Polling", end="", flush=True)
    while True:
        time.sleep(5)
        job = api("GET", f"/jobs/{job_id}")
        status = job["status"]
        msg = job.get("message") or ""
        print(f"\r  [{status}] {msg:<60}", end="", flush=True)
        if status == "complete":
            print()
            break
        if status == "failed":
            print(f"\n\nJob failed: {job.get('error')}", file=sys.stderr)
            sys.exit(1)

    # Download
    out_filename = job["output_filename"]
    dur = job.get("duration_seconds")
    dur_str = f" ({dur:.1f}s)" if dur else ""
    print(f"  Downloading {out_filename}{dur_str} …", end=" ", flush=True)
    download_url = f"{BASE_URL}/download/{out_filename}"
    urllib.request.urlretrieve(download_url, args.out)
    print(f"saved to {args.out}")
    print(f"\nDone! Output: {args.out}\n")


if __name__ == "__main__":
    main()
