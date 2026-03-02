import os
import re
import uuid
import logging
import asyncio
import tempfile
import shutil
import base64
from pathlib import Path
from enum import Enum
from typing import Optional

from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.responses import JSONResponse
import aiofiles

logger = logging.getLogger(__name__)

app = FastAPI(title="Audio Splitter Service")

SUPPORTED_FORMATS = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".wma", ".opus"}
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


def get_mime_type(ext: str) -> str:
    """Get MIME type for audio extension."""
    mime_types = {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".flac": "audio/flac",
        ".ogg": "audio/ogg",
        ".m4a": "audio/x-m4a",
        ".aac": "audio/aac",
        ".wma": "audio/x-ms-wma",
        ".opus": "audio/opus",
    }
    return mime_types.get(ext, "audio/octet-stream")


def calculate_segment_time(chunk_size_mb: float, bitrate_kbps: int = 320) -> float:
    """Calculate segment time in seconds based on chunk size and estimated bitrate."""
    chunk_size_bits = chunk_size_mb * 1024 * 1024 * 8
    return chunk_size_bits / (bitrate_kbps * 1000)


async def get_audio_duration(file_path: str) -> Optional[float]:
    """Get audio duration using ffprobe."""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
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
        logger.error("ffprobe duration timed out for file: %s", file_path)
        return None
    try:
        return float(stdout.decode().strip())
    except (ValueError, AttributeError):
        return None


async def get_audio_bitrate(file_path: str) -> int:
    """Get audio bitrate using ffprobe, default to 320kbps if not found."""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=bit_rate",
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
        logger.error("ffprobe bitrate timed out for file: %s", file_path)
        return 320
    try:
        return int(stdout.decode().strip()) // 1000
    except (ValueError, AttributeError):
        return 320


async def split_audio(
    input_path: str,
    output_dir: str,
    chunk_size_mb: float,
    output_prefix: str,
    same_as_input: bool,
    output_format: Optional[str] = None,
    correlation_id: str = ""
) -> list[str]:
    """Split audio file using FFmpeg segment muxer."""
    input_ext = get_file_extension(input_path)

    if same_as_input:
        ext = input_ext
    elif output_format:
        ext = f".{output_format.lstrip('.')}"
    else:
        ext = input_ext

    bitrate = await get_audio_bitrate(input_path)
    segment_time = calculate_segment_time(chunk_size_mb, bitrate)

    output_pattern = os.path.join(output_dir, f"{output_prefix}_%03d{ext}")

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

    verified_chunks = []
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
        verified_chunks.append(chunk_path)

    if len(verified_chunks) > MAX_CHUNKS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Too many chunks generated ({len(verified_chunks)}). "
                f"Maximum allowed: {MAX_CHUNKS}. "
                "Increase chunk_size_mb to reduce the number of chunks."
            )
        )

    return verified_chunks


async def get_chunk_data(chunk_path: str, original_filename: str, original_duration: float) -> dict:
    """Get metadata and binary data for a single chunk file."""
    filename = os.path.basename(chunk_path)
    ext = get_file_extension(filename)
    size = os.path.getsize(chunk_path)
    mime_type = get_mime_type(ext)

    async with aiofiles.open(chunk_path, 'rb') as f:
        binary_data = await f.read()

    return {
        "data": {
            "filename": filename,
            "fileExtension": ext.lstrip('.'),
            "mimeType": mime_type,
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
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
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
    output_format: Optional[str] = Form(default=None, description="Output format if not same as input"),
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

    if chunk_size_mb < CHUNK_SIZE_MB_MIN or chunk_size_mb > CHUNK_SIZE_MB_MAX:
        raise HTTPException(
            status_code=400,
            detail=(
                f"chunk_size_mb must be between {CHUNK_SIZE_MB_MIN} "
                f"and {CHUNK_SIZE_MB_MAX}"
            )
        )

    if not same_as_input and output_format:
        normalized_format = f".{output_format.lstrip('.')}"
        if normalized_format not in SUPPORTED_FORMATS:
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
        os.makedirs(output_dir, exist_ok=True)

        # AUTO mode always streams since file size is unknown upfront
        use_streaming = memory_mode in (MemoryMode.AUTO, MemoryMode.STREAMING)

        if use_streaming:
            file_size = 0
            async with aiofiles.open(input_path, 'wb') as f:
                while content := await file.read(1024 * 1024):
                    file_size += len(content)
                    if file_size > MAX_FILE_SIZE:
                        raise HTTPException(
                            status_code=413,
                            detail=f"File too large. Max size: {MAX_FILE_SIZE // (1024*1024)}MB"
                        )
                    await f.write(content)
        else:
            content = await file.read()
            if len(content) > MAX_FILE_SIZE:
                raise HTTPException(
                    status_code=413,
                    detail=f"File too large. Max size: {MAX_FILE_SIZE // (1024*1024)}MB"
                )
            async with aiofiles.open(input_path, 'wb') as f:
                await f.write(content)

        original_duration = await get_audio_duration(input_path) or 0.0

        chunks = await split_audio(
            input_path=input_path,
            output_dir=output_dir,
            chunk_size_mb=chunk_size_mb,
            output_prefix=output_prefix,
            same_as_input=same_as_input,
            output_format=output_format,
            correlation_id=correlation_id
        )

        if not chunks:
            raise HTTPException(status_code=500, detail="No chunks generated")

        result = []
        for chunk_path in chunks:
            chunk_data = await get_chunk_data(chunk_path, original_filename, original_duration)
            result.append(chunk_data)

        return JSONResponse(content=result)

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
