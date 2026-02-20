"""
Multi-Language Video Dubbing Web App — FastAPI Backend

Endpoints:
  POST /api/process       — upload an MP4 + target language to start dubbing
  GET  /api/status/{id}   — SSE stream of job progress
  GET  /api/download/{id} — download the finished file
"""

import asyncio
import uuid
import shutil
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

from pipeline import jobs, run_pipeline
from utils import get_job_dir, check_ffmpeg, cleanup_job

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="Multi-Language Video Dubbing", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "ffmpeg_available": check_ffmpeg(),
    }


@app.post("/api/process")
async def start_process(
    file: UploadFile = File(...),
    target_language: str = Form(...),
):
    """Start a new dubbing job with an uploaded video file."""
    # Validate file type
    if not file.filename or not file.filename.lower().endswith(('.mp4', '.mkv', '.webm', '.avi', '.mov')):
        raise HTTPException(
            status_code=400,
            detail="Please upload a video file (.mp4, .mkv, .webm, .avi, .mov)"
        )

    if not check_ffmpeg():
        raise HTTPException(status_code=500, detail="FFmpeg is not installed or not found on PATH")

    # Create job
    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {
        "status": "queued",
        "progress": 0,
        "step": "queued",
        "message": "Job queued, saving uploaded file...",
        "error": None,
        "output_file": None,
    }

    # Save uploaded file to job directory
    job_dir = get_job_dir(job_id)
    video_path = job_dir / "source.mp4"

    try:
        with open(video_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save uploaded file: {e}")
    finally:
        await file.close()

    # Launch pipeline in background
    asyncio.create_task(run_pipeline(job_id, str(video_path), target_language.lower()))

    return {"job_id": job_id}


@app.get("/api/status/{job_id}")
async def job_status_sse(job_id: str):
    """Stream job progress via Server-Sent Events."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    async def event_generator():
        prev_msg = ""
        while True:
            job = jobs.get(job_id, {})
            status = job.get("status", "unknown")
            progress = job.get("progress", 0)
            message = job.get("message", "")
            step = job.get("step", "")
            error = job.get("error")

            # Only send if something changed
            current_msg = f"{status}|{progress}|{step}|{message}"
            if current_msg != prev_msg:
                import json
                data = json.dumps({
                    "status": status,
                    "progress": progress,
                    "step": step,
                    "message": message,
                    "error": error,
                })
                yield f"data: {data}\n\n"
                prev_msg = current_msg

            if status in ("completed", "failed"):
                break

            await asyncio.sleep(1)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/download/{job_id}")
async def download_result(job_id: str):
    """Download the dubbed output file."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[job_id]
    if job.get("status") != "completed":
        raise HTTPException(status_code=400, detail="Job is not completed yet")

    output_file = job.get("output_file")
    if not output_file or not Path(output_file).exists():
        raise HTTPException(status_code=404, detail="Output file not found")

    return FileResponse(
        path=output_file,
        filename=f"dubbed_{job_id}.mp4",
        media_type="video/mp4",
    )


@app.delete("/api/cleanup/{job_id}")
async def cleanup(job_id: str):
    """Clean up temporary files for a job."""
    cleanup_job(job_id)
    jobs.pop(job_id, None)
    return {"status": "cleaned"}
