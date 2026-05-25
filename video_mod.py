#!/usr/bin/env python3
"""
Video Mod Tool

A video modification toolkit: upscale, sharpen, blur, and enhance.

Capabilities:
  - Upscale to 4K / 2K / 1440p / 1080p or custom resolution (Lanczos)
  - Upscale images by smallest dimension (--upscale-image)
  - AI upscaling via Real-ESRGAN or OpenCV DNN super-resolution
  - Sharpen with unsharp mask (adjustable strength & radius)
  - Gaussian blur with adjustable percentage (0-100%)
  - Orton effect: dreamy glow via screen+multiply blend (use with --blur)
  - Detail enhancement pass
  - High-quality H.264/H.265 encoding

Usage:
    python video_mod.py -i video.mp4 -o video_4k.mp4 -r 4k
    python video_mod.py -i video.mp4 -o video_ai.mp4 -r 4k --ai-upscale
    python video_mod.py -i video.mp4 -o video_ai.mp4 -r 4k --ai-upscale --ai-backend dnn --model-path EDSR_x4.pb
    python video_mod.py -i video.mp4 -o sharp.mp4 --sharpen-only
    python video_mod.py -i video.mp4 -o blurred.mp4 --blur 50
    python video_mod.py -i video.mp4 -o orton.mp4 --blur 40 --orton
    python video_mod.py -i video.mp4 -o orton.mp4 --blur 40 --orton --orton-strength 0.8
    python video_mod.py -i video.mp4 -o combo.mp4 -r 4k --blur 20 -ss 1.5
    python video_mod.py --upscale-image photo.png --min-dim 4000
    python video_mod.py --upscale-image *.png --min-dim 2160 --frames-dir output/

Dependencies:
    pip install opencv-python numpy
    ffmpeg must be installed (brew install ffmpeg)
    AI upscaling (realesrgan backend): pip install realesrgan
"""

from __future__ import annotations

# Hot-patch torchvision.transforms.functional_tensor to prevent basicsr
# import crash in modern environments (torchvision >= 0.15.0)
import sys
try:
    import torchvision.transforms.functional_tensor
except ImportError:
    try:
        import torchvision.transforms.functional as functional
        sys.modules['torchvision.transforms.functional_tensor'] = functional
    except ImportError:
        pass

import argparse
import os
import subprocess
import sys
import shutil
import time

import cv2
import numpy as np


# ---------------------------------------------------------------------------
# AI upscaling helpers
# ---------------------------------------------------------------------------

def _nearest_ai_scale(src_w, src_h, target_w, target_h, available):
    """Return the smallest scale in *available* that reaches the target size."""
    needed = max(target_w / src_w, target_h / src_h)
    for s in sorted(available):
        if s >= needed - 0.05:
            return s
    return max(available)


def build_dnn_upscaler(model_path: str):
    """
    Load an OpenCV DNN super-resolution model.
    Algorithm name and scale are inferred from the filename,
    e.g. "EDSR_x4.pb" → algorithm="edsr", scale=4.
    Returns (sr_object, scale).
    """
    fname = os.path.basename(model_path)
    parts = os.path.splitext(fname)[0].split("_")
    algo  = parts[0].lower()
    try:
        scale = int(parts[-1].lstrip("x"))
    except ValueError:
        sys.exit(f"Could not parse scale from model filename: {fname}\n"
                 "Expected format: ALGO_xSCALE.pb  e.g. EDSR_x4.pb")
    sr = cv2.dnn_superres.DnnSuperResImpl_create()
    sr.readModel(model_path)
    sr.setModel(algo, scale)
    print(f"  DNN super-res: {algo.upper()} x{scale} ({model_path})")
    return sr, scale


def build_realesrgan_upscaler(ai_model: str = "anime", tile_size: int | None = None):
    """
    Build a Real-ESRGAN 4x upsampler.
    ai_model="anime" → realesr-animevideov3  (anime/CGI video, temporal consistency)
    ai_model="photo" → RealESRGAN_x4plus     (photographic content)
    Weights are downloaded automatically on first use.
    Returns the RealESRGANer object (always 4x).
    """
    try:
        from realesrgan import RealESRGANer
        from basicsr.archs.rrdbnet_arch import RRDBNet
    except ImportError:
        sys.exit(
            "Real-ESRGAN not installed.\n"
            "Install with: pip install realesrgan"
        )

    if ai_model == "anime":
        from basicsr.archs.srvgg_arch import SRVGGNetCompact
        arch = SRVGGNetCompact(num_in_ch=3, num_out_ch=3, num_feat=64,
                               num_conv=16, upscale=4, act_type='prelu')
        weights = (
            "https://github.com/xinntao/Real-ESRGAN/releases/download/"
            "v0.2.5.0/realesr-animevideov3.pth"
        )
    else:
        arch = RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64,
                       num_block=23, num_grow_ch=32, scale=4)
        weights = (
            "https://github.com/xinntao/Real-ESRGAN/releases/download/"
            "v0.1.0/RealESRGAN_x4plus.pth"
        )

    try:
        import torch
        if torch.cuda.is_available():
            device = torch.device("cuda")
            half = True
            tile = 512
        elif torch.backends.mps.is_available():
            device = torch.device("mps")
            half = False  # MPS doesn't support fp16 reliably
            tile = 1024   # larger tiles = fewer GPU dispatches = faster on MPS
        else:
            device = torch.device("cpu")
            half = False
            tile = 512
    except ImportError:
        device = None
        half = False
        tile = 512

    if tile_size is not None:
        tile = tile_size

    kwargs = dict(
        scale=4,
        model_path=weights,
        model=arch,
        tile=tile,
        tile_pad=10,
        pre_pad=0,
        half=half,
    )
    if device is not None:
        kwargs["device"] = device

    upsampler = RealESRGANer(**kwargs)
    device_label = str(device).upper() if device else "CPU"
    print(f"  Real-ESRGAN x4 [{ai_model} model] on {device_label} (tile={tile})")
    return upsampler


def build_realesrgan_restorer(ai_model: str = "general", tile_size: int | None = None):
    """
    Build a Real-ESRGAN restorer that removes pixelation/compression artifacts
    and sharpens at the *original* resolution (outscale=1 at inference time).
    ai_model="general" → realesr-general-x4v3  (best for mixed/real content)
    ai_model="anime"   → RealESRGAN_x4plus_anime_6B
    ai_model="photo"   → RealESRGAN_x4plus
    """
    try:
        from realesrgan import RealESRGANer
        from basicsr.archs.rrdbnet_arch import RRDBNet
    except ImportError:
        sys.exit(
            "Real-ESRGAN not installed.\n"
            "Install with: pip install realesrgan"
        )

    if ai_model == "general":
        try:
            from realesrgan.archs.srvgg_arch import SRVGGNetCompact
            arch = SRVGGNetCompact(num_in_ch=3, num_out_ch=3, num_feat=64,
                                   num_conv=32, upscale=4, act_type="prelu")
        except ImportError:
            # fallback to photo model if SRVGGNetCompact unavailable
            arch = RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64,
                           num_block=23, num_grow_ch=32, scale=4)
            ai_model = "photo"
        weights = (
            "https://github.com/xinntao/Real-ESRGAN/releases/download/"
            "v0.2.5.0/realesr-general-x4v3.pth"
        )
    elif ai_model == "anime":
        arch = RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64,
                       num_block=6, num_grow_ch=32, scale=4)
        weights = (
            "https://github.com/xinntao/Real-ESRGAN/releases/download/"
            "v0.2.2.4/RealESRGAN_x4plus_anime_6B.pth"
        )
    else:  # photo
        arch = RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64,
                       num_block=23, num_grow_ch=32, scale=4)
        weights = (
            "https://github.com/xinntao/Real-ESRGAN/releases/download/"
            "v0.1.0/RealESRGAN_x4plus.pth"
        )

    try:
        import torch
        if torch.cuda.is_available():
            device = torch.device("cuda")
            half = True
            tile = 512
        elif torch.backends.mps.is_available():
            device = torch.device("mps")
            half = False
            tile = 1024
        else:
            device = torch.device("cpu")
            half = False
            tile = 512
    except ImportError:
        device = None
        half = False
        tile = 512

    if tile_size is not None:
        tile = tile_size

    kwargs = dict(
        scale=4,
        model_path=weights,
        model=arch,
        tile=tile,
        tile_pad=10,
        pre_pad=0,
        half=half,
    )
    if device is not None:
        kwargs["device"] = device

    restorer = RealESRGANer(**kwargs)
    device_label = str(device).upper() if device else "CPU"
    print(f"  Real-ESRGAN restore [{ai_model} model] on {device_label}")
    return restorer


# ---------------------------------------------------------------------------
# Checkpoint / resume helpers
# ---------------------------------------------------------------------------

def _count_temp_frames(temp_path: str) -> int:
    """Count frames in an existing temp video file using ffprobe."""
    if not os.path.isfile(temp_path):
        return 0
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-count_frames",
             "-select_streams", "v:0",
             "-show_entries", "stream=nb_read_frames",
             "-print_format", "csv=p=0", temp_path],
            capture_output=True, text=True, timeout=60
        )
        return int(result.stdout.strip())
    except (ValueError, subprocess.TimeoutExpired):
        return 0


def _write_checkpoint(checkpoint_path: str, frame_num: int):
    """Write current frame number to checkpoint file."""
    with open(checkpoint_path, "w") as f:
        f.write(str(frame_num))


def _read_checkpoint(checkpoint_path: str) -> int:
    """Read frame number from checkpoint file, or 0 if missing/invalid."""
    try:
        with open(checkpoint_path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Resolution presets
# ---------------------------------------------------------------------------
PRESETS = {
    "4k":    (3840, 2160),
    "2k":    (2560, 1440),
    "1440p": (2560, 1440),
    "1080p": (1920, 1080),
    "720p":  (1280, 720),
}


# ---------------------------------------------------------------------------
# Sharpening filters
# ---------------------------------------------------------------------------

def unsharp_mask(frame: np.ndarray, strength: float = 1.0,
                 radius: float = 1.5) -> np.ndarray:
    """
    Apply unsharp mask sharpening.
    Blurs the image, then adds back the difference scaled by strength.
    """
    ksize = int(radius * 4) | 1  # ensure odd
    blurred = cv2.GaussianBlur(frame, (ksize, ksize), radius)
    sharpened = cv2.addWeighted(frame, 1.0 + strength, blurred, -strength, 0)
    return np.clip(sharpened, 0, 255).astype(np.uint8)


def gaussian_blur(frame: np.ndarray, percent: float) -> np.ndarray:
    """
    Apply Gaussian blur at a given percentage.
    0% = no blur, 100% = maximum blur.
    The kernel size scales with the percentage.
    """
    if percent <= 0:
        return frame
    percent = min(percent, 100.0)
    # Map percentage to kernel radius: 0% → 0, 100% → ~50px
    radius = percent * 0.5
    ksize = int(radius * 2) | 1  # ensure odd, minimum 1
    ksize = max(3, ksize)
    return cv2.GaussianBlur(frame, (ksize, ksize), radius * 0.5)


def orton_effect(frame: np.ndarray, blur_percent: float,
                 strength: float = 0.7) -> np.ndarray:
    """
    Apply the Orton effect for a dreamy, ethereal glow.

    The effect works in three steps:
      1. Screen-blend the frame with itself → over-bright bloomy layer
      2. Gaussian-blur the screened layer  → soft spreading glow
      3. Multiply-blend the original with the blurred layer → restores
         contrast/shadows while the highlights glow

    The result is mixed back against the original using `strength` so the
    effect can be dialled from subtle (0.3) to full (1.0).

    blur_percent  – same scale as --blur; controls the glow radius
    strength      – 0.0 = no effect, 1.0 = full Orton blend (default 0.7)
    """
    if blur_percent <= 0:
        return frame

    f = frame.astype(np.float32)

    # Step 1: screen blend frame with itself (A screen A = 255-(255-A)^2/255)
    screened = 255.0 - (255.0 - f) * (255.0 - f) / 255.0
    screened_u8 = np.clip(screened, 0, 255).astype(np.uint8)

    # Step 2: blur the screened layer to spread the glow
    blurred = gaussian_blur(screened_u8, blur_percent)

    # Step 3: multiply-blend original × blurred-screen / 255
    orton = f * blurred.astype(np.float32) / 255.0
    orton_u8 = np.clip(orton, 0, 255).astype(np.uint8)

    # Mix with original at the requested strength
    return cv2.addWeighted(frame, 1.0 - strength, orton_u8, strength, 0)


def detail_enhance(frame: np.ndarray, sigma_s: float = 10,
                   sigma_r: float = 0.15) -> np.ndarray:
    """
    Enhance fine details using edge-preserving filtering.
    """
    return cv2.detailEnhance(frame, sigma_s=sigma_s, sigma_r=sigma_r)


def upscale_image(
    input_path: str,
    output_path: str | None = None,
    min_dim: int = 2160,
    interpolation: int = cv2.INTER_LANCZOS4,
    ai_upscaler=None,
) -> np.ndarray:
    """
    Upscale an image so its smallest dimension equals *min_dim* pixels.
    Aspect ratio is preserved. If the image is already large enough it is
    returned unchanged (no downscaling).

    When *ai_upscaler* is provided (a RealESRGANer instance) the image is
    first enlarged by the AI model (4x) to preserve detail, then resized
    to the exact target dimensions with Lanczos. Without an AI upscaler,
    plain Lanczos interpolation is used.

    Parameters
    ----------
    input_path : str
        Path to the source image.
    output_path : str, optional
        Where to save the result.  When *None* the image is returned but
        not written to disk.
    min_dim : int
        Target size in pixels for the smallest side (default 2160).
    interpolation : int
        OpenCV interpolation flag (default INTER_LANCZOS4).
    ai_upscaler : optional
        A RealESRGANer instance for AI-based upscaling.  Built once by the
        caller and reused across multiple images.

    Returns
    -------
    np.ndarray
        The (possibly upscaled) image in BGR format.
    """
    img = cv2.imread(input_path, cv2.IMREAD_UNCHANGED)
    if img is None:
        sys.exit(f"Error: cannot read image: {input_path}")

    h, w = img.shape[:2]
    smallest = min(w, h)

    if smallest >= min_dim:
        print(f"  {os.path.basename(input_path)}: already {w}x{h} "
              f"(smallest side {smallest} >= {min_dim}), skipped")
        if output_path:
            cv2.imwrite(output_path, img)
        return img

    scale = min_dim / smallest
    new_w = int(round(w * scale))
    new_h = int(round(h * scale))
    # Ensure even dimensions (useful for video encoding later)
    new_w = new_w + (new_w % 2)
    new_h = new_h + (new_h % 2)

    if ai_upscaler is not None:
        # AI upscale (4x) then resize to exact target
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        enhanced, _ = ai_upscaler.enhance(rgb, outscale=4)
        img = cv2.cvtColor(enhanced, cv2.COLOR_RGB2BGR)
        # Resize to the exact target if AI output doesn't match
        if img.shape[1] != new_w or img.shape[0] != new_h:
            img = cv2.resize(img, (new_w, new_h), interpolation=interpolation)
        print(f"  {os.path.basename(input_path)}: {w}x{h} → {new_w}x{new_h} (AI)")
    else:
        img = cv2.resize(img, (new_w, new_h), interpolation=interpolation)
        print(f"  {os.path.basename(input_path)}: {w}x{h} → {new_w}x{new_h}")

    if output_path:
        cv2.imwrite(output_path, img)

    return img


def center_crop(frame: np.ndarray, ratio_w: int, ratio_h: int) -> np.ndarray:
    """
    Center-crop a frame to the given aspect ratio (ratio_w:ratio_h).
    Keeps the largest possible area and ensures even dimensions.
    """
    h, w = frame.shape[:2]
    target_aspect = ratio_w / ratio_h
    current_aspect = w / h

    if current_aspect > target_aspect:
        # Too wide — crop width
        new_w = int(h * target_aspect)
        new_w = new_w - (new_w % 2)  # ensure even
        x_off = (w - new_w) // 2
        return frame[:, x_off:x_off + new_w]
    else:
        # Too tall — crop height
        new_h = int(w / target_aspect)
        new_h = new_h - (new_h % 2)  # ensure even
        y_off = (h - new_h) // 2
        return frame[y_off:y_off + new_h, :]


# ---------------------------------------------------------------------------
# Main processing pipeline
# ---------------------------------------------------------------------------

def process_video(
    input_path: str,
    output_path: str,
    target_w: int,
    target_h: int,
    sharpen_strength: float = 1.0,
    sharpen_radius: float = 1.5,
    blur_percent: float = 0.0,
    orton: bool = False,
    orton_strength: float = 0.7,
    enhance_details: bool = False,
    sharpen_only: bool = False,
    codec: str = "h264",
    crf: int = 18,
    ai_upscale: bool = False,
    ai_backend: str = "realesrgan",
    ai_model: str = "anime",
    model_path: str = "",
    frames_dir: str = "",
    ai_restore: bool = False,
    ai_restore_model: str = "general",
    frames_only: bool = False,
    crop_ratio: tuple[int, int] | None = None,
    tile_size: int | None = None,
):
    """
    Read input video, upscale and process each frame, write to temp file,
    then mux with original audio using ffmpeg.

    When ai_upscale=True the frame is first enlarged by the AI model to at
    least the target size, then Lanczos-resized to the exact target if needed.
    The Lanczos fallback is still used when ai_upscale=False.
    """
    if not shutil.which("ffmpeg"):
        sys.exit("Error: ffmpeg not found. Install it with: brew install ffmpeg")

    process_start = time.time()

    cap = cv2.VideoCapture(input_path)
    if not cap.isOpened():
        sys.exit(f"Error: cannot open video: {input_path}")

    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    if sharpen_only:
        target_w, target_h = src_w, src_h

    # Compute output dimensions after crop
    do_crop = crop_ratio is not None
    if do_crop:
        cw, ch = crop_ratio
        crop_aspect = cw / ch
        cur_aspect = target_w / target_h
        if cur_aspect > crop_aspect:
            # Too wide — crop width after processing
            out_w = int(target_h * crop_aspect)
            out_w = out_w - (out_w % 2)
            out_h = target_h
        else:
            # Too tall — crop height after processing
            out_h = int(target_w / crop_aspect)
            out_h = out_h - (out_h % 2)
            out_w = target_w
        print(f"Crop:   {cw}:{ch} → {out_w}x{out_h}")
    else:
        out_w, out_h = target_w, target_h

    do_sharpen = sharpen_strength > 0
    do_blur = blur_percent > 0
    do_orton = orton and blur_percent > 0

    print(f"Input:  {src_w}x{src_h} @ {fps:.2f}fps, {total_frames} frames")
    print(f"Output: {out_w}x{out_h}")
    if ai_upscale:
        print(f"Upscale: AI ({ai_backend})")
    if ai_restore:
        print(f"Restore: AI depixelate/sharpen ({ai_restore_model} model)")
    if do_sharpen:
        print(f"Sharpen: strength={sharpen_strength}, radius={sharpen_radius}")
    if do_orton:
        print(f"Orton effect: blur={blur_percent:.0f}%, strength={orton_strength:.2f}")
    elif do_blur:
        print(f"Gaussian blur: {blur_percent:.0f}%")
    if enhance_details:
        print("Detail enhancement: enabled")

    # ── Build AI upscaler (once, before the frame loop) ───────────────────
    ai_sr = None      # OpenCV DNN super-res object
    ai_esrgan = None  # Real-ESRGAN upsampler
    ai_scale = 1

    if ai_upscale and not sharpen_only and (target_w != src_w or target_h != src_h):
        if ai_backend == "dnn":
            if not model_path:
                sys.exit(
                    "Error: --model-path is required for the dnn backend.\n"
                    "Download an EDSR/ESPCN/FSRCNN/LapSRN model (.pb) from the\n"
                    "OpenCV extra modules repository and pass its path here."
                )
            if not os.path.isfile(model_path):
                sys.exit(f"Error: model file not found: {model_path}")
            ai_sr, ai_scale = build_dnn_upscaler(model_path)
        else:
            ai_esrgan = build_realesrgan_upscaler(ai_model, tile_size=tile_size)
            ai_scale = 4
    elif ai_upscale and sharpen_only:
        print("  Note: --ai-upscale ignored with --sharpen-only (no resize)")

    ai_restorer = None
    if ai_restore:
        ai_restorer = build_realesrgan_restorer(ai_restore_model, tile_size=tile_size)

    needs_upscale = (target_w != src_w or target_h != src_h)

    # ── Frame directory setup (save PNGs for crash recovery / resume) ─────
    save_frames = bool(frames_dir)
    resume_from = 0
    if save_frames:
        os.makedirs(frames_dir, exist_ok=True)
        existing = sorted(f for f in os.listdir(frames_dir) if f.endswith(".png"))
        if existing:
            resume_from = len(existing)
            print(f"  Resuming from frame {resume_from} ({resume_from} frames already in {frames_dir})")
        else:
            print(f"  Saving frames to: {frames_dir}")

    # ── Checkpoint-based resume (no frames dir needed) ────────────────────
    fourcc = cv2.VideoWriter_fourcc(*"MJPG")
    temp_ext = output_path + ".tmp.avi"
    checkpoint_path = output_path + ".checkpoint"
    checkpoint_resume = 0
    temp_parts = []  # list of temp file paths to concatenate at the end

    if not save_frames and os.path.isfile(temp_ext) and os.path.isfile(checkpoint_path):
        saved_frame = _read_checkpoint(checkpoint_path)
        if saved_frame > 0:
            # Verify the temp file has roughly the expected frames
            actual_frames = _count_temp_frames(temp_ext)
            if actual_frames > 0 and actual_frames >= saved_frame - 100:
                checkpoint_resume = min(saved_frame, actual_frames)
                print(f"  Checkpoint found: resuming from frame {checkpoint_resume} "
                      f"({actual_frames} frames in existing temp)")
                # Keep the old temp as part 1, write new frames to part 2
                temp_parts.append(temp_ext)
                temp_ext = output_path + ".tmp2.avi"
                # Seek input past already-processed frames
                cap.set(cv2.CAP_PROP_POS_FRAMES, checkpoint_resume)
            else:
                print(f"  Checkpoint found but temp file mismatch "
                      f"(checkpoint={saved_frame}, actual={actual_frames}). "
                      f"Starting fresh.")
                os.remove(temp_ext)
                os.remove(checkpoint_path)
    elif not save_frames:
        # Clean stale checkpoint if no temp file exists
        if os.path.isfile(checkpoint_path) and not os.path.isfile(temp_ext):
            os.remove(checkpoint_path)

    # Write processed frames to a temp video (no audio)
    # Use MJPEG for lossless intermediate to avoid double-compression
    writer = cv2.VideoWriter(temp_ext, fourcc, fps, (out_w, out_h))

    if not writer.isOpened():
        # Fallback to mp4v
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        temp_ext = output_path + ".tmp.mp4" if not temp_parts else output_path + ".tmp2.mp4"
        writer = cv2.VideoWriter(temp_ext, fourcc, fps, (out_w, out_h))
    if not writer.isOpened():
        sys.exit("Error: could not create video writer")

    frame_num = checkpoint_resume
    frame_times = []
    print(f"\nProcessing frames...")

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        # Skip already-processed frames when resuming (frames-dir mode)
        if save_frames and frame_num < resume_from:
            png_path = os.path.join(frames_dir, f"frame_{frame_num:06d}.png")
            saved = cv2.imread(png_path)
            if saved is not None:
                writer.write(saved)
                frame_num += 1
                if frame_num % 100 == 0 or frame_num == total_frames:
                    pct = 100 * frame_num / max(total_frames, 1)
                    print(f"  {frame_num}/{total_frames} ({pct:.0f}%) [resumed]")
                continue

        t0 = time.time()

        # 1. Upscale – AI or Lanczos
        if needs_upscale:
            if ai_sr is not None:
                # DNN super-res: operates on BGR uint8
                frame = ai_sr.upsample(frame)
                # Trim to exact target if AI scale overshot
                if frame.shape[1] != target_w or frame.shape[0] != target_h:
                    frame = cv2.resize(frame, (target_w, target_h),
                                       interpolation=cv2.INTER_LANCZOS4)
            elif ai_esrgan is not None:
                # Real-ESRGAN expects RGB; returns RGB uint8
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                enhanced, _ = ai_esrgan.enhance(rgb, outscale=4)
                frame = cv2.cvtColor(enhanced, cv2.COLOR_RGB2BGR)
                if frame.shape[1] != target_w or frame.shape[0] != target_h:
                    frame = cv2.resize(frame, (target_w, target_h),
                                       interpolation=cv2.INTER_LANCZOS4)
            else:
                frame = cv2.resize(frame, (target_w, target_h),
                                   interpolation=cv2.INTER_LANCZOS4)

        # 2. AI restore (depixelate / remove compression artifacts)
        if ai_restorer is not None:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            restored, _ = ai_restorer.enhance(rgb, outscale=1)
            frame = cv2.cvtColor(restored, cv2.COLOR_RGB2BGR)

        # 3. Blur / Orton effect
        if do_orton:
            frame = orton_effect(frame, blur_percent, orton_strength)
        elif do_blur:
            frame = gaussian_blur(frame, blur_percent)

        # 4. Unsharp mask
        if do_sharpen:
            frame = unsharp_mask(frame, sharpen_strength, sharpen_radius)

        # 5. Optional detail enhancement
        if enhance_details:
            frame = detail_enhance(frame)

        # 6. Crop to target aspect ratio
        if do_crop:
            frame = center_crop(frame, crop_ratio[0], crop_ratio[1])

        if save_frames:
            png_path = os.path.join(frames_dir, f"frame_{frame_num:06d}.png")
            cv2.imwrite(png_path, frame)

        writer.write(frame)

        elapsed = time.time() - t0
        frame_times.append(elapsed)
        frame_num += 1

        # Write checkpoint every 100 frames (lightweight resume support)
        if not save_frames and frame_num % 100 == 0:
            _write_checkpoint(checkpoint_path, frame_num)

        if frame_num == 1 or frame_num % 10 == 0 or frame_num == total_frames:
            pct = 100 * frame_num / max(total_frames, 1)
            avg = sum(frame_times) / len(frame_times)
            remaining = avg * (total_frames - frame_num)
            mins, secs = divmod(int(remaining), 60)
            bar_filled = int(pct / 5)   # 20-char bar
            bar = "#" * bar_filled + "-" * (20 - bar_filled)
            print(f"  [{bar}] {pct:5.1f}%  "
                  f"frame {frame_num}/{total_frames}  "
                  f"{elapsed:.2f}s/frame  ETA {mins}m{secs:02d}s")

    cap.release()
    writer.release()
    print(f"  Processed {frame_num} frames")

    # ── Concatenate temp parts if resuming from checkpoint ─────────────────
    if temp_parts:
        temp_parts.append(temp_ext)  # add the new part
        concat_temp = output_path + ".concat.txt"
        with open(concat_temp, "w") as f:
            for part in temp_parts:
                f.write(f"file '{os.path.abspath(part)}'\n")
        merged_temp = output_path + ".tmp_merged.avi"
        merge_cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", concat_temp, "-c", "copy", merged_temp,
        ]
        merge_result = subprocess.run(merge_cmd, capture_output=True, text=True)
        os.remove(concat_temp)
        if merge_result.returncode != 0:
            print(f"Warning: failed to merge temp parts, using last segment only")
            merged_temp = temp_ext
        else:
            # Clean up individual parts
            for part in temp_parts:
                if os.path.isfile(part):
                    os.remove(part)
        temp_ext = merged_temp

    # Clean up checkpoint file on successful completion
    if os.path.isfile(checkpoint_path):
        os.remove(checkpoint_path)

    if frames_only:
        os.remove(temp_ext)
        total_secs = int(time.time() - process_start)
        h, rem = divmod(total_secs, 3600)
        m, s = divmod(rem, 60)
        elapsed_str = f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"
        print(f"\n{'='*50}")
        print(f"  Frames saved : {frames_dir}")
        print(f"  Frame count  : {frame_num}  ({fps:.2f} fps)")
        print(f"  Time         : {elapsed_str}")
        print(f"\n  To compile into a video run:")
        print(f"    python video_mod.py --combine-frames {frames_dir} "
              f"-i \"{input_path}\" -o \"{output_path}\"")
        print(f"{'='*50}")
        return

    # --- Mux processed video with original audio via ffmpeg ---
    print(f"\nEncoding final video with ffmpeg ({codec}, CRF {crf})...")

    vcodec = "libx265" if codec.lower() in ("h265", "hevc") else "libx264"
    pix_fmt = "yuv420p"

    cmd = [
        "ffmpeg", "-y",
        "-i", temp_ext,           # processed video (no audio)
        "-i", input_path,         # original (for audio)
        "-map", "0:v:0",         # video from processed
        "-map", "1:a:0?",        # audio from original (if exists)
        "-c:v", vcodec,
        "-crf", str(crf),
        "-preset", "slow",
        "-pix_fmt", pix_fmt,
        "-c:a", "aac",
        "-b:a", "320k",
        "-movflags", "+faststart",
        output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ffmpeg stderr:\n{result.stderr}")
        sys.exit(f"Error: ffmpeg encoding failed (exit code {result.returncode})")

    # Clean up temp file
    os.remove(temp_ext)

    total_secs = int(time.time() - process_start)
    h, rem = divmod(total_secs, 3600)
    m, s = divmod(rem, 60)
    elapsed_str = f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"

    in_mb  = os.path.getsize(input_path)  / (1024 * 1024)
    out_mb = os.path.getsize(output_path) / (1024 * 1024)

    print(f"\n{'='*50}")
    print(f"  Output : {output_path}")
    print(f"  Size   : {out_mb:.1f} MB  (input was {in_mb:.1f} MB)")
    print(f"  Frames : {frame_num}  ({fps:.2f} fps)")
    print(f"  Time   : {elapsed_str}")
    print(f"{'='*50}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Video modification tool: upscale, sharpen, blur, enhance."
    )
    parser.add_argument(
        "--input", "-i", default="",
        help="Input video file.",
    )
    parser.add_argument(
        "--output", "-o", default="",
        help="Output video file.",
    )
    parser.add_argument(
        "--resolution", "-r",
        choices=list(PRESETS.keys()),
        default=None,
        help="Target resolution preset (e.g. 4k, 1440p, 1080p).",
    )
    parser.add_argument("--width", "-W", type=int, default=None)
    parser.add_argument("--height", "-H", type=int, default=None)
    parser.add_argument(
        "--sharpen-strength", "-ss", type=float, default=1.0,
        help="Unsharp mask strength (default: 1.0, range 0.5-3.0).",
    )
    parser.add_argument(
        "--sharpen-radius", "-sr", type=float, default=1.5,
        help="Unsharp mask radius in pixels (default: 1.5).",
    )
    parser.add_argument(
        "--enhance-details", "-e", action="store_true",
        help="Enable extra detail enhancement pass.",
    )
    parser.add_argument(
        "--blur", "-b", type=float, default=0.0,
        help="Blur percentage (0=none, 50=moderate, 100=max). Controls glow "
             "radius when --orton is set. Default: 0.",
    )
    parser.add_argument(
        "--orton", action="store_true",
        help="Apply Orton effect instead of plain blur (requires --blur > 0). "
             "Blends a screen+multiply glow with the original for a dreamy look.",
    )
    parser.add_argument(
        "--orton-strength", type=float, default=0.7,
        help="Orton blend strength: 0.0=no effect, 1.0=full glow (default: 0.7).",
    )
    parser.add_argument(
        "--no-sharpen", action="store_true",
        help="Disable sharpening (useful when only blurring or upscaling).",
    )
    parser.add_argument(
        "--sharpen-only", action="store_true",
        help="Apply effects without changing resolution.",
    )
    parser.add_argument(
        "--codec", choices=["h264", "h265"], default="h264",
        help="Output codec (default: h264). h265 = smaller files.",
    )
    parser.add_argument(
        "--crf", type=int, default=18,
        help="Quality (0=lossless, 18=high, 23=default, 28=low). Default: 18.",
    )
    parser.add_argument(
        "--ai-upscale", action="store_true",
        help="Use an AI super-resolution model instead of Lanczos for upscaling. "
             "Produces sharper detail, especially on synthetic/rendered content. "
             "Default backend: realesrgan (pip install realesrgan).",
    )
    parser.add_argument(
        "--ai-backend", choices=["realesrgan", "dnn"], default="realesrgan",
        help="AI upscaling backend. 'realesrgan' (default) gives the best quality "
             "and downloads weights automatically. 'dnn' uses OpenCV's built-in "
             "DNN super-res and requires --model-path.",
    )
    parser.add_argument(
        "--ai-model", choices=["anime", "photo"], default="anime",
        help="Real-ESRGAN model variant. 'anime' (default) is trained on "
             "synthetic/CGI content and works best for fractal video. "
             "'photo' is trained on real-world photographs. "
             "Ignored when --ai-backend=dnn.",
    )
    parser.add_argument(
        "--model-path", default="",
        metavar="PATH",
        help="Path to an OpenCV DNN super-res model file (.pb), e.g. EDSR_x4.pb. "
             "Required when --ai-backend=dnn. Download model files from the "
             "opencv_extra repository (LapSRN / EDSR / ESPCN / FSRCNN).",
    )
    parser.add_argument(
        "--tile", type=int, default=None,
        metavar="PX",
        help="Tile size for Real-ESRGAN inference. Larger tiles are faster but "
             "use more VRAM. Default: 1024 (MPS), 512 (CUDA/CPU). "
             "Try 0 to process the whole frame at once (fastest, needs enough VRAM).",
    )
    parser.add_argument(
        "--frames-dir", default="",
        metavar="DIR",
        help="Save each processed frame as a PNG in DIR. If DIR already contains "
             "frames from a previous run, processing resumes where it left off. "
             "Frames are kept after encoding so you can re-encode without reprocessing.",
    )
    parser.add_argument(
        "--combine-frames", default="",
        metavar="DIR",
        help="Skip processing — combine PNGs from DIR directly into the output video. "
             "Requires --input (for audio) and --output. "
             "Example: --combine-frames frames/ -i original.mp4 -o output.mp4",
    )
    parser.add_argument(
        "--ai-restore", action="store_true",
        help="AI depixelation and sharpening at original resolution. Uses Real-ESRGAN "
             "internally at 4x then downsamples back — removes compression artifacts "
             "and pixelation without changing the video size.",
    )
    parser.add_argument(
        "--ai-restore-model", choices=["general", "anime", "photo"], default="general",
        help="Model for --ai-restore. 'general' (default) works best for most content. "
             "'anime' for CGI/rendered. 'photo' for real footage.",
    )
    parser.add_argument(
        "--frames-only", action="store_true",
        help="Process and save frames to --frames-dir, then stop without encoding. "
             "Edit the frames, then use --combine-frames to compile the final video.",
    )
    parser.add_argument(
        "--crop", default=None, metavar="W:H",
        help="Center-crop to an aspect ratio (e.g. 9:16 for portrait, 1:1 for square). "
             "Applied after upscaling/effects.",
    )
    parser.add_argument(
        "--upscale-frames", default="",
        metavar="DIR",
        help="Upscale all PNGs in DIR to 4K (or -r preset). Use --ai-upscale for AI "
             "quality. Saves in place or to --frames-dir if specified. "
             "Example: --upscale-frames frames/ -r 4k --ai-upscale",
    )
    parser.add_argument(
        "--check-frames", default="",
        metavar="DIR",
        help="Check a frames directory for gaps in sequence numbering. "
             "Reports missing frame numbers and total gap count, then exits. "
             "Example: --check-frames frames_4k/",
    )
    parser.add_argument(
        "--upscale-image", nargs="+", default=None,
        metavar="PATH",
        help="Upscale one or more images so the smallest dimension equals "
             "--min-dim pixels (default 2160). Aspect ratio is preserved. "
             "Output is saved next to the original with '_upscaled' suffix, "
             "or to --output / --frames-dir. "
             "Example: --upscale-image photo.png --min-dim 4000",
    )
    parser.add_argument(
        "--min-dim", type=int, default=2160,
        metavar="PX",
        help="Target pixel size for the smallest dimension when using "
             "--upscale-image (default: 2160).",
    )

    args = parser.parse_args()

    # ── Check-frames mode: report gaps in frame numbering ──────────────
    if args.check_frames:
        if not os.path.isdir(args.check_frames):
            sys.exit(f"Error: frames directory not found: {args.check_frames}")
        pngs = sorted(f for f in os.listdir(args.check_frames) if f.endswith(".png"))
        if not pngs:
            sys.exit(f"Error: no PNG frames found in {args.check_frames}")
        import re
        numbers = []
        for f in pngs:
            m = re.search(r"(\d+)", f)
            if m:
                numbers.append(int(m.group(1)))
        numbers.sort()
        print(f"Frames directory: {args.check_frames}")
        print(f"Total frames found: {len(numbers)}")
        print(f"Range: {numbers[0]} → {numbers[-1]} (expected {numbers[-1] - numbers[0] + 1})")
        gaps = []
        for i in range(1, len(numbers)):
            if numbers[i] != numbers[i-1] + 1:
                gap_start = numbers[i-1] + 1
                gap_end = numbers[i] - 1
                gaps.append((gap_start, gap_end))
        if not gaps:
            print("No gaps — sequence is complete.")
        else:
            total_missing = sum(end - start + 1 for start, end in gaps)
            print(f"\nFound {len(gaps)} gap(s), {total_missing} missing frame(s):\n")
            for start, end in gaps:
                if start == end:
                    print(f"  Missing: frame {start}")
                else:
                    print(f"  Missing: frames {start}–{end} ({end - start + 1} frames)")
        return

    # ── Combine-frames mode: reassemble PNGs → video without reprocessing ──
    if args.combine_frames:
        if not os.path.isdir(args.combine_frames):
            sys.exit(f"Error: frames directory not found: {args.combine_frames}")
        frames = sorted(f for f in os.listdir(args.combine_frames) if f.endswith(".png"))
        if not frames:
            sys.exit(f"Error: no PNG frames found in {args.combine_frames}")
        print(f"Combining {len(frames)} frames from {args.combine_frames} → {args.output}")
        codec_name = "libx265" if args.codec.lower() in ("h265", "hevc") else "libx264"
        fps = 30.0
        # Use input video framerate if provided
        if args.input and os.path.isfile(args.input):
            cap = cv2.VideoCapture(args.input)
            fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
            cap.release()
        # Build a concat file listing every frame (handles gaps in numbering)
        frame_dir = os.path.abspath(args.combine_frames)
        concat_path = os.path.join(frame_dir, "_concat_list.txt")
        frame_dur = f"{1/fps:.10f}"
        with open(concat_path, "w") as f:
            for fname in frames:
                safe = os.path.join(frame_dir, fname).replace("'", "'\\''")
                f.write(f"file '{safe}'\n")
                f.write(f"duration {frame_dur}\n")
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", concat_path,
        ]
        if args.input and os.path.isfile(args.input):
            cmd += ["-i", args.input, "-map", "0:v:0", "-map", "1:a:0?",
                    "-c:a", "aac", "-b:a", "320k"]
        cmd += [
            "-c:v", codec_name,
            "-crf", str(args.crf),
            "-preset", "slow",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-r", str(fps),
            args.output,
        ]
        result = subprocess.run(cmd, capture_output=True)
        os.remove(concat_path)
        if result.returncode != 0:
            print(f"ffmpeg stderr:\n{result.stderr.decode('utf-8', errors='replace')}")
            sys.exit(f"Error: ffmpeg failed (exit code {result.returncode})")
        size_mb = os.path.getsize(args.output) / (1024 * 1024)
        print(f"Done! Saved to: {args.output} ({size_mb:.1f} MB)")
        return

    # ── Upscale-image mode: scale images by smallest dimension ─────────
    if args.upscale_image:
        out_dir = args.frames_dir if args.frames_dir else None
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        # Build AI upscaler (default on; skip with --no-ai)
        ai_esrgan = None
        if not getattr(args, 'no_ai', False):
            ai_esrgan = build_realesrgan_upscaler(args.ai_model, tile_size=args.tile)

        mode_label = "AI" if ai_esrgan else "Lanczos"
        print(f"Upscaling {len(args.upscale_image)} image(s), "
              f"smallest dimension → {args.min_dim}px ({mode_label})")

        for img_path in args.upscale_image:
            if not os.path.isfile(img_path):
                print(f"  Warning: file not found, skipping: {img_path}")
                continue

            if args.output and len(args.upscale_image) == 1:
                dst = args.output
            elif out_dir:
                dst = os.path.join(out_dir, os.path.basename(img_path))
            else:
                base, ext = os.path.splitext(img_path)
                dst = f"{base}_upscaled{ext}"

            upscale_image(img_path, dst, min_dim=args.min_dim,
                          ai_upscaler=ai_esrgan)

        print("Done!")
        return

    # ── Upscale-frames mode: upscale each PNG in DIR to 4K (or -r preset) ──
    if args.upscale_frames:
        if not os.path.isdir(args.upscale_frames):
            sys.exit(f"Error: frames directory not found: {args.upscale_frames}")
        frames = sorted(f for f in os.listdir(args.upscale_frames) if f.endswith(".png"))
        if not frames:
            sys.exit(f"Error: no PNG frames found in {args.upscale_frames}")

        # Determine target resolution
        if args.resolution:
            target_w, target_h = PRESETS[args.resolution]
        elif args.width and args.height:
            target_w, target_h = args.width, args.height
        else:
            target_w, target_h = PRESETS["4k"]
            print("No resolution specified, defaulting to 4K (3840x2160)")

        # Build AI upscaler if requested
        ai_esrgan = None
        if args.ai_upscale:
            ai_esrgan = build_realesrgan_upscaler(args.ai_model)

        out_dir = args.frames_dir if args.frames_dir else args.upscale_frames
        if out_dir != args.upscale_frames:
            os.makedirs(out_dir, exist_ok=True)

        print(f"Upscaling {len(frames)} frames → {target_w}x{target_h}")
        print(f"Output dir: {out_dir}")

        frame_times = []
        for i, fname in enumerate(frames, 1):
            src_path = os.path.join(args.upscale_frames, fname)
            dst_path = os.path.join(out_dir, fname)
            t0 = time.time()

            frame = cv2.imread(src_path)
            if frame is None:
                print(f"  Warning: could not read {fname}, skipping")
                continue

            if ai_esrgan is not None:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                enhanced, _ = ai_esrgan.enhance(rgb, outscale=4)
                frame = cv2.cvtColor(enhanced, cv2.COLOR_RGB2BGR)

            if frame.shape[1] != target_w or frame.shape[0] != target_h:
                frame = cv2.resize(frame, (target_w, target_h),
                                   interpolation=cv2.INTER_LANCZOS4)

            cv2.imwrite(dst_path, frame)

            elapsed = time.time() - t0
            frame_times.append(elapsed)
            pct = 100 * i / len(frames)
            avg = sum(frame_times) / len(frame_times)
            remaining = avg * (len(frames) - i)
            mins, secs = divmod(int(remaining), 60)
            bar_filled = int(pct / 5)
            bar = "#" * bar_filled + "-" * (20 - bar_filled)
            print(f"  [{bar}] {pct:5.1f}%  "
                  f"frame {i}/{len(frames)}  "
                  f"{elapsed:.2f}s/frame  ETA {mins}m{secs:02d}s")

        print(f"\nDone! {len(frames)} frames upscaled to {out_dir}")
        print(f"To compile: python video_mod.py --combine-frames {out_dir} "
              f"-i <original.mp4> -o <output.mp4>")
        return

    if args.frames_only and not args.frames_dir:
        sys.exit("Error: --frames-only requires --frames-dir")

    # Normal processing mode requires both -i and -o
    if not args.input:
        sys.exit("Error: --input/-i is required")
    if not args.output:
        sys.exit("Error: --output/-o is required")

    if not os.path.isfile(args.input):
        sys.exit(f"Error: input file not found: {args.input}")

    # Determine target resolution
    if args.sharpen_only:
        target_w, target_h = 0, 0  # will be set from source
    elif args.resolution:
        target_w, target_h = PRESETS[args.resolution]
    elif args.width and args.height:
        target_w, target_h = args.width, args.height
    elif args.width:
        # Calculate height maintaining aspect ratio
        cap = cv2.VideoCapture(args.input)
        sw = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
        sh = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
        cap.release()
        target_w = args.width
        target_h = int(args.width * sh / sw)
        target_h = target_h + (target_h % 2)  # ensure even
    else:
        if args.crop:
            # Crop-only: keep source resolution, just crop
            cap = cv2.VideoCapture(args.input)
            target_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            target_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            cap.release()
        else:
            target_w, target_h = PRESETS["4k"]
            print("No resolution specified, defaulting to 4K (3840x2160)")

    # Parse crop ratio
    crop_ratio = None
    if args.crop:
        parts = args.crop.split(":")
        if len(parts) != 2:
            sys.exit("Error: --crop must be W:H (e.g. 9:16)")
        try:
            crop_ratio = (int(parts[0]), int(parts[1]))
        except ValueError:
            sys.exit("Error: --crop must be W:H with integer values (e.g. 9:16)")

    # AI upscaling already handles detail; suppress default sharpening unless
    # the user explicitly asked for it via --sharpen-strength or left default.
    if args.ai_upscale and not args.no_sharpen and args.sharpen_strength == 1.0:
        sharpen_str = 0.0
    else:
        sharpen_str = 0.0 if args.no_sharpen else args.sharpen_strength

    process_video(
        input_path=args.input,
        output_path=args.output,
        target_w=target_w,
        target_h=target_h,
        sharpen_strength=sharpen_str,
        sharpen_radius=args.sharpen_radius,
        blur_percent=args.blur,
        orton=args.orton,
        orton_strength=args.orton_strength,
        enhance_details=args.enhance_details,
        sharpen_only=args.sharpen_only,
        codec=args.codec,
        crf=args.crf,
        ai_upscale=args.ai_upscale,
        ai_backend=args.ai_backend,
        ai_model=args.ai_model,
        model_path=args.model_path,
        frames_dir=args.frames_dir,
        ai_restore=args.ai_restore,
        ai_restore_model=args.ai_restore_model,
        frames_only=args.frames_only,
        crop_ratio=crop_ratio,
        tile_size=args.tile,
    )


if __name__ == "__main__":
    main()
