import asyncio
import base64
import logging
import os
import re
import shutil
import tempfile
import uuid
from enum import Enum
from pathlib import Path

import aiofiles
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

app = FastAPI(title="Audio Splitter Service")

MIME_TYPES = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".m4a": "audio/x-m4a",
    ".aac": "audio/aac",
    ".wma": "audio/x-ms-wma",
    ".opus": "audio/opus",
}
SUPPORTED_FORMATS = MIME_TYPES.keys()  # a view, not a set: keeps the "Supported: ..." order stable
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE_MB", "500")) * 1024 * 1024
FFMPEG_TIMEOUT_SECONDS = int(os.getenv("FFMPEG_TIMEOUT_SECONDS", "300"))
FFPROBE_TIMEOUT_SECONDS = int(os.getenv("FFPROBE_TIMEOUT_SECONDS", "30"))
CHUNK_SIZE_MB_MIN = float(os.getenv("CHUNK_SIZE_MB_MIN", "0.1"))
CHUNK_SIZE_MB_MAX = float(os.getenv("CHUNK_SIZE_MB_MAX", "500"))
MAX_CHUNKS = int(os.getenv("MAX_CHUNKS", "1000"))
MAX_OUTPUT_PREFIX_LENGTH = 64


class MemoryMode(str, Enum):
    AUTO = "auto"
    STREAMING = "streaming"
    BUFFERED = "buffered"


def sanitize_prefix(prefix: str) -> str:
    """Sanitize output prefix to prevent path traversal."""
    sanitized = re.sub(r'[^a-zA-Z0-9_-]', '_', prefix)
    return sanitized[:MAX_OUTPUT_PREFIX_LENGTH]


def get_file_extension(filename: str) -> str:
    return Path(filename).suffix.lower()


def calculate_segment_time(chunk_size_mb: float, bitrate_kbps: int) -> float:
    """Calculate segment time in seconds based on chunk size and estimated bitrate."""
    chunk_size_bits = chunk_size_mb * 1024 * 1024 * 8
    return chunk_size_bits / (bitrate_kbps * 1000)


async def ffprobe_format(file_path: str, entry: str, label: str) -> bytes:
    """Run ffprobe for one format entry; return its raw output, or b"" on timeout.

    `label` names the entry in the timeout log line ("bitrate" for "bit_rate").
    """
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", f"format={entry}",
        "-of", "default=noprint_wrappers=1:nokey=1", file_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=FFPROBE_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        logger.error("ffprobe %s timed out for file: %s", label, file_path)
        return b""
    return stdout


async def get_audio_duration(file_path: str) -> float:
    """Get audio duration using ffprobe, default to 0.0 if not found."""
    stdout = await ffprobe_format(file_path, "duration", "duration")
    try:
        return float(stdout.decode().strip()) or 0.0  # normalizes -0.0 to 0.0
    except ValueError:
        return 0.0


async def get_audio_bitrate(file_path: str) -> int:
    """Get audio bitrate using ffprobe, default to 320kbps if not found."""
    stdout = await ffprobe_format(file_path, "bit_rate", "bitrate")
    try:
        return int(stdout.decode().strip()) // 1000 or 320  # <1 kbps would divide by zero
    except ValueError:
        return 320


async def split_audio(
    input_path: str,
    output_dir: str,
    chunk_size_mb: float,
    output_prefix: str,
    output_ext: str,
    correlation_id: str
) -> list[str]:
    """Split audio file using FFmpeg segment muxer."""
    bitrate = await get_audio_bitrate(input_path)
    segment_time = calculate_segment_time(chunk_size_mb, bitrate)

    output_pattern = os.path.join(output_dir, f"{output_prefix}_%03d{output_ext}")

    cmd = [
        "ffmpeg", "-i", input_path,
        "-f", "segment",
        "-segment_time", str(segment_time),
        "-c", "copy",
        "-map", "0:a",
        "-reset_timestamps", "1",
        output_pattern
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    try:
        _, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=FFMPEG_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        logger.error(
            "FFmpeg timed out after %ds [ref: %s]",
            FFMPEG_TIMEOUT_SECONDS, correlation_id
        )
        raise HTTPException(
            status_code=500,
            detail=f"Audio processing timed out. Reference: {correlation_id}"
        )

    if proc.returncode != 0:
        logger.error(
            "FFmpeg failed [ref: %s]: %s",
            correlation_id, stderr.decode()
        )
        raise HTTPException(
            status_code=500,
            detail=f"Audio processing failed. Reference: {correlation_id}"
        )

    real_output_dir = os.path.realpath(output_dir)
    chunks = sorted([
        os.path.join(real_output_dir, f)
        for f in os.listdir(real_output_dir)
        if f.startswith(output_prefix)
    ])

    for chunk_path in chunks:
        real_chunk = os.path.realpath(chunk_path)
        if not real_chunk.startswith(real_output_dir + os.sep):
            logger.error(
                "Path traversal detected in output [ref: %s]: %s",
                correlation_id, real_chunk
            )
            raise HTTPException(
                status_code=500,
                detail=f"Audio processing failed. Reference: {correlation_id}"
            )

    if len(chunks) > MAX_CHUNKS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Too many chunks generated ({len(chunks)}). "
                f"Maximum allowed: {MAX_CHUNKS}. "
                "Increase chunk_size_mb to reduce the number of chunks."
            )
        )

    return chunks


async def get_chunk_data(chunk_path: str, original_filename: str, original_duration: float) -> dict:
    """Get metadata and binary data for a single chunk file."""
    filename = os.path.basename(chunk_path)
    ext = get_file_extension(filename)

    async with aiofiles.open(chunk_path, 'rb') as f:
        binary_data = await f.read()
    size = len(binary_data)

    return {
        "data": {
            "filename": filename,
            "fileExtension": ext.lstrip('.'),
            "mimeType": MIME_TYPES.get(ext, "audio/octet-stream"),
            "size": size,
            "sizeInMB": size / (1024 * 1024),
            "originalFile": original_filename,
            "duration": original_duration,
        },
        "binary": base64.b64encode(binary_data).decode('utf-8')
    }


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy"}


@app.get("/ready")
async def readiness_check():
    """Readiness check - verify FFmpeg is available."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
    except OSError:  # missing or non-executable ffmpeg
        raise HTTPException(status_code=503, detail="FFmpeg not available")
    try:
        await asyncio.wait_for(
            proc.communicate(), timeout=FFPROBE_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise HTTPException(status_code=503, detail="FFmpeg not available")

    if proc.returncode != 0:
        raise HTTPException(status_code=503, detail="FFmpeg not available")

    return {"status": "ready"}


@app.post("/split")
async def split_audio_endpoint(
    file: UploadFile = File(..., description="Audio file to split"),
    chunk_size_mb: float = Form(default=10.0, description="Size of each chunk in MB"),
    output_prefix: str = Form(default="chunk", description="Prefix for chunk filenames"),
    same_as_input: bool = Form(default=True, description="Use same format as input"),
    output_format: str | None = Form(default=None, description="Output format if not same as input"),
    memory_mode: MemoryMode = Form(default=MemoryMode.AUTO, description="Memory management mode")
):
    """
    Split an audio file into chunks of specified size.

    Returns JSON array with metadata and base64-encoded binary data for each chunk.
    """
    correlation_id = str(uuid.uuid4())
    original_filename = file.filename or "audio.mp3"
    ext = get_file_extension(original_filename)

    if ext not in SUPPORTED_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported format: {ext}. Supported: {', '.join(SUPPORTED_FORMATS)}"
        )

    output_prefix = sanitize_prefix(output_prefix)

    if not (CHUNK_SIZE_MB_MIN <= chunk_size_mb <= CHUNK_SIZE_MB_MAX):  # also rejects NaN
        raise HTTPException(
            status_code=400,
            detail=(
                f"chunk_size_mb must be between {CHUNK_SIZE_MB_MIN} "
                f"and {CHUNK_SIZE_MB_MAX}"
            )
        )

    output_ext = ext
    if not same_as_input and output_format:
        output_ext = f".{output_format.lstrip('.').lower()}"
        if output_ext not in SUPPORTED_FORMATS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unsupported output format: {output_format}. "
                    f"Supported: {', '.join(f.lstrip('.') for f in SUPPORTED_FORMATS)}"
                )
            )

    work_dir = tempfile.mkdtemp()

    try:
        input_path = os.path.join(work_dir, f"input{ext}")
        output_dir = os.path.join(work_dir, "chunks")
        os.makedirs(output_dir)

        too_large_detail = f"File too large. Max size: {MAX_FILE_SIZE // (1024*1024)}MB"
        # AUTO is an alias of STREAMING
        if memory_mode in (MemoryMode.AUTO, MemoryMode.STREAMING):
            file_size = 0
            async with aiofiles.open(input_path, 'wb') as f:
                while content := await file.read(1024 * 1024):
                    file_size += len(content)
                    if file_size > MAX_FILE_SIZE:
                        raise HTTPException(status_code=413, detail=too_large_detail)
                    await f.write(content)
        else:
            content = await file.read()
            if len(content) > MAX_FILE_SIZE:
                raise HTTPException(status_code=413, detail=too_large_detail)
            async with aiofiles.open(input_path, 'wb') as f:
                await f.write(content)

        original_duration = await get_audio_duration(input_path)

        chunks = await split_audio(
            input_path=input_path,
            output_dir=output_dir,
            chunk_size_mb=chunk_size_mb,
            output_prefix=output_prefix,
            output_ext=output_ext,
            correlation_id=correlation_id
        )

        if not chunks:
            logger.error("No chunks generated [ref: %s]", correlation_id)
            raise HTTPException(
                status_code=500,
                detail=f"Audio processing failed. Reference: {correlation_id}"
            )

        return JSONResponse(content=[
            await get_chunk_data(chunk_path, original_filename, original_duration)
            for chunk_path in chunks
        ])

    except HTTPException:
        raise
    except Exception:
        logger.exception("Unhandled error [ref: %s]", correlation_id)
        raise HTTPException(
            status_code=500,
            detail=f"Internal server error. Reference: {correlation_id}"
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
