"""
Frame Extractor & Interesting Image Analyzer for Music Video Pipeline.

Analyzes YouTube videos (long videos and shorts) to identify visually rich,
aesthetic, sharp, and diverse keyframes, and exports them in full resolution.
"""

from __future__ import annotations

import os
import math
import cv2
import numpy as np
from PIL import Image


def compute_frame_metrics(frame_bgr: np.ndarray) -> dict[str, float]:
    """
    Computes visual interestingness heuristics for a single BGR frame.
    
    Metrics:
      - sharpness: Laplacian variance (higher = sharper, more in-focus)
      - colorfulness: Hasler-Süsstrunk metric (higher = more vibrant color distribution)
      - contrast: RMS contrast of luminance channel (higher = richer dynamic range)
      - entropy: Shannon entropy of luminance (higher = richer visual information/detail)
      - exposure_score: Gaussian penalty centered at mean luminance ~128 (penalizes pitch black or clipped white)
      - edge_density: Sobel gradient energy (higher = finer texture/structural complexity)
    """
    # 1. Luminance & Grayscale
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape

    # Sharpness (Laplacian variance)
    # Using 64-bit float to prevent overflow
    laplacian = cv2.Laplacian(gray, cv2.CV_64F)
    sharpness = float(laplacian.var())

    # Colorfulness (Hasler and Süsstrunk metric)
    # Split channels (B, G, R)
    b, g, r = cv2.split(frame_bgr.astype(np.float32))
    rg = np.abs(r - g)
    yb = np.abs(0.5 * (r + g) - b)
    
    std_rg = float(np.std(rg))
    std_yb = float(np.std(yb))
    mean_rg = float(np.mean(rg))
    mean_yb = float(np.mean(yb))
    
    std_rgyb = math.sqrt(std_rg ** 2 + std_yb ** 2)
    mean_rgyb = math.sqrt(mean_rg ** 2 + mean_yb ** 2)
    colorfulness = std_rgyb + (0.3 * mean_rgyb)

    # Contrast (RMS contrast)
    mean_lum = float(np.mean(gray))
    contrast = float(np.std(gray))

    # Exposure penalty (0.0 to 1.0)
    # Peak at mean_lum = 120-135, dropping to 0 near 0 or 255
    if mean_lum < 15 or mean_lum > 245:
        exposure_score = 0.05
    else:
        # Gaussian curve centered at 128 with sigma ~55
        exposure_score = math.exp(-((mean_lum - 128.0) ** 2) / (2.0 * (55.0 ** 2)))

    # Shannon Entropy of luminance
    hist = cv2.calcHist([gray], [0], None, [256], [0, 256]).ravel()
    hist_norm = hist / max(1.0, hist.sum())
    hist_norm = hist_norm[hist_norm > 0]
    entropy = float(-np.sum(hist_norm * np.log2(hist_norm)))  # 0 to 8 bits

    # Edge density / structural complexity (Sobel)
    sobelx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    sobely = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    edge_mag = cv2.magnitude(sobelx, sobely)
    edge_density = float(np.mean(edge_mag))

    # Visual Fingerprints for Deduplication
    # 1. 2D HSV Color Histogram (16x16)
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    hsv_hist = cv2.calcHist([hsv], [0, 1], None, [16, 16], [0, 180, 0, 256])
    cv2.normalize(hsv_hist, hsv_hist, alpha=0, beta=1, norm_type=cv2.NORM_MINMAX)

    # 2. Structural normalized feature vector (16x16)
    tiny = cv2.resize(gray, (16, 16), interpolation=cv2.INTER_AREA).astype(np.float32)
    std_val = float(np.std(tiny))
    tiny_norm = (tiny - float(np.mean(tiny))) / (std_val if std_val > 1e-4 else 1.0)

    return {
        "sharpness": sharpness,
        "colorfulness": colorfulness,
        "contrast": contrast,
        "mean_lum": mean_lum,
        "exposure_score": exposure_score,
        "entropy": entropy,
        "edge_density": edge_density,
        "hsv_hist": hsv_hist,
        "tiny_norm": tiny_norm,
    }


def compute_visual_similarity(candA: dict, candB: dict) -> float:
    """
    Computes visual similarity score (0.0 to 1.0) between two candidate frame fingerprints.
    Combines 2D HSV color distribution correlation and 16x16 structural texture similarity.
    """
    histA = candA.get("hsv_hist")
    histB = candB.get("hsv_hist")
    if histA is not None and histB is not None:
        color_sim = cv2.compareHist(histA, histB, cv2.HISTCMP_CORREL)
        color_sim = max(0.0, float(color_sim))
    else:
        color_sim = 0.0

    tinyA = candA.get("tiny_norm")
    tinyB = candB.get("tiny_norm")
    if tinyA is not None and tinyB is not None:
        struct_sim = float(np.mean(tinyA * tinyB))
        struct_sim = max(0.0, float(struct_sim))
    else:
        struct_sim = 0.0

    # If both color and structural patterns match strongly, return high similarity
    if color_sim > 0.90:
        return 0.7 * color_sim + 0.3 * struct_sim

    return 0.5 * color_sim + 0.5 * struct_sim


def analyze_interesting_frames(
    video_path: str,
    n_candidates: int = 10,
    min_separation: float = 4.0,
    max_similarity: float = 0.72,
    max_samples: int = 400,
    sample_interval: float | None = None,
    progress_callback=None,
) -> list[dict]:
    """
    Scans a video file, evaluates frame interestingness heuristics, and returns
    the top K most interesting, diverse keyframes without duplicates.
    
    Returns list of dicts with:
      - rank: int (1-based)
      - timestamp_sec: float
      - time_str: str (MM:SS.ss)
      - overall_score: float (0.0 to 100.0)
      - sharpness: float
      - colorfulness: float
      - contrast: float
      - entropy: float
      - width: int
      - height: int
      - thumbnail_pil: PIL.Image
    """
    if not os.path.isfile(video_path):
        raise FileNotFoundError(f"Video file not found: {video_path}")

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video file: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    duration_sec = total_frames / fps if fps > 0 and total_frames > 0 else 0.0

    if duration_sec <= 0:
        cap.release()
        raise RuntimeError(f"Invalid video duration or empty video: {video_path}")

    # Determine sampling interval
    if sample_interval is None or sample_interval <= 0:
        # Dynamically target between 100 and max_samples
        sample_interval = max(0.4, duration_sec / max_samples)

    sample_times = np.arange(0.5, max(0.6, duration_sec - 0.5), sample_interval)
    if len(sample_times) == 0:
        sample_times = np.array([duration_sec / 2.0])

    num_samples = len(sample_times)
    candidates_raw: list[dict] = []

    # Target thumbnail width for fast analysis
    analysis_width = 480

    for i, t in enumerate(sample_times):
        if progress_callback:
            pct = int(10 + 60 * (i / num_samples))
            progress_callback(f"Scanning frames ({i + 1}/{num_samples})…", pct)

        cap.set(cv2.CAP_PROP_POS_MSEC, float(t * 1000.0))
        ret, frame = cap.read()
        if not ret or frame is None:
            continue

        # Downscale for analysis speed
        fh, fw = frame.shape[:2]
        if fw > analysis_width:
            scale = analysis_width / fw
            small_frame = cv2.resize(frame, (analysis_width, int(fh * scale)), interpolation=cv2.INTER_AREA)
        else:
            small_frame = frame

        metrics = compute_frame_metrics(small_frame)
        metrics["timestamp_sec"] = float(t)
        metrics["orig_width"] = width or fw
        metrics["orig_height"] = height or fh
        candidates_raw.append(metrics)

    # Normalize metrics across the video distribution (0 to 1 min-max scaling)
    def normalize(vals: list[float]) -> np.ndarray:
        arr = np.array(vals, dtype=np.float32)
        min_v = np.min(arr)
        max_v = np.max(arr)
        if max_v - min_v > 1e-6:
            return (arr - min_v) / (max_v - min_v)
        return np.ones_like(arr) * 0.5

    sharpness_norm = normalize([c["sharpness"] for c in candidates_raw])
    color_norm = normalize([c["colorfulness"] for c in candidates_raw])
    contrast_norm = normalize([c["contrast"] for c in candidates_raw])
    entropy_norm = normalize([c["entropy"] for c in candidates_raw])
    edge_norm = normalize([c["edge_density"] for c in candidates_raw])

    # Compute overall interestingness score (0 to 100)
    for i, c in enumerate(candidates_raw):
        # Weights:
        # Sharpness: 0.25, Colorfulness: 0.25, Contrast: 0.20, Entropy: 0.15, Edge Density: 0.15
        base_score = (
            0.25 * sharpness_norm[i]
            + 0.25 * color_norm[i]
            + 0.20 * contrast_norm[i]
            + 0.15 * entropy_norm[i]
            + 0.15 * edge_norm[i]
        )
        # Factor in exposure penalty (frames with extreme darkness/blowout are penalized)
        final_score = base_score * (0.3 + 0.7 * c["exposure_score"]) * 100.0
        c["overall_score"] = float(final_score)

    if progress_callback:
        progress_callback("Deduplicating & selecting top distinct keyframes…", 75)

    # Sort all candidates by score descending
    candidates_raw.sort(key=lambda x: x["overall_score"], reverse=True)

    # Visual & Temporal Deduplication:
    # A candidate is selected ONLY if it is separated in time AND visually distinct from all already selected frames.
    selected: list[dict] = []
    for cand in candidates_raw:
        t = cand["timestamp_sec"]
        
        is_duplicate = False
        for sel in selected:
            # 1. Temporal closeness check
            if abs(t - sel["timestamp_sec"]) < min_separation:
                is_duplicate = True
                break
            # 2. Visual similarity check (catches looping video repeats and slow-moving identical scenes)
            similarity = compute_visual_similarity(cand, sel)
            if similarity >= max_similarity:
                is_duplicate = True
                break

        if not is_duplicate:
            selected.append(cand)
            if len(selected) >= n_candidates:
                break

    # If we couldn't fill n_candidates due to strict thresholds, relax slightly
    if len(selected) < n_candidates and len(candidates_raw) > len(selected):
        relaxed_sep = min_separation / 2.0
        relaxed_sim = min(0.92, max_similarity + 0.15)
        for cand in candidates_raw:
            if cand in selected:
                continue
            t = cand["timestamp_sec"]
            is_dup = False
            for sel in selected:
                if abs(t - sel["timestamp_sec"]) < relaxed_sep:
                    is_dup = True
                    break
                if compute_visual_similarity(cand, sel) >= relaxed_sim:
                    is_dup = True
                    break
            if not is_dup:
                selected.append(cand)
                if len(selected) >= n_candidates:
                    break

    # Format results & pre-extract high quality thumbnails
    results: list[dict] = []
    max_thumb_dim = 280

    for rank_idx, c in enumerate(selected):
        t_sec = c["timestamp_sec"]
        m, s = divmod(t_sec, 60.0)
        time_str = f"{int(m):02d}:{s:05.2f}"

        # Extract thumbnail frame directly while video is open
        thumb_pil = None
        cap.set(cv2.CAP_PROP_POS_MSEC, float(t_sec * 1000.0))
        ret, frame_bgr = cap.read()
        if ret and frame_bgr is not None:
            rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            fh, fw = rgb.shape[:2]
            if fw >= fh:
                scale = max_thumb_dim / fw
            else:
                scale = max_thumb_dim / fh
            nw, nh = max(1, int(fw * scale)), max(1, int(fh * scale))
            small = cv2.resize(rgb, (nw, nh), interpolation=cv2.INTER_AREA)
            thumb_pil = Image.fromarray(small)

        results.append({
            "rank": rank_idx + 1,
            "timestamp_sec": round(t_sec, 3),
            "time_str": time_str,
            "overall_score": round(c["overall_score"], 1),
            "sharpness": round(c["sharpness"], 1),
            "colorfulness": round(c["colorfulness"], 1),
            "contrast": round(c["contrast"], 1),
            "entropy": round(c["entropy"], 2),
            "width": c["orig_width"],
            "height": c["orig_height"],
            "thumbnail_pil": thumb_pil,
        })

    cap.release()

    if progress_callback:
        progress_callback("Done", 100)

    return results


def extract_full_frame(video_path: str, timestamp_sec: float) -> np.ndarray | None:
    """
    Extracts a pristine full-resolution frame (RGB format) at the exact timestamp.
    """
    if not os.path.isfile(video_path):
        return None

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return None

    cap.set(cv2.CAP_PROP_POS_MSEC, float(timestamp_sec * 1000.0))
    ret, frame_bgr = cap.read()
    cap.release()

    if not ret or frame_bgr is None:
        return None

    # Convert BGR to RGB
    return cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)


def extract_thumbnail_image(video_path: str, timestamp_sec: float, max_dim: int = 360) -> Image.Image | None:
    """
    Extracts a frame and scales it to a PIL Image suitable for UI preview.
    Preserves aspect ratio (for both 16:9 widescreen and 9:16 vertical shorts).
    """
    rgb = extract_full_frame(video_path, timestamp_sec)
    if rgb is None:
        return None

    h, w = rgb.shape[:2]
    if w >= h:
        scale = max_dim / w
    else:
        scale = max_dim / h

    nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
    small = cv2.resize(rgb, (nw, nh), interpolation=cv2.INTER_AREA)
    return Image.fromarray(small)


def export_frame(
    video_path: str,
    timestamp_sec: float,
    output_path: str,
    img_format: str = "png",
    jpeg_quality: int = 95,
) -> bool:
    """
    Extracts the full-resolution frame at timestamp_sec and saves it to output_path.
    Supported formats: 'png', 'jpg' / 'jpeg', 'webp'.
    """
    rgb = extract_full_frame(video_path, timestamp_sec)
    if rgb is None:
        return False

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    pil_img = Image.fromarray(rgb)

    fmt = img_format.lower().replace(".", "")
    if fmt in ("jpg", "jpeg"):
        pil_img.save(output_path, format="JPEG", quality=jpeg_quality, subsampling=0)
    elif fmt == "webp":
        pil_img.save(output_path, format="WEBP", quality=jpeg_quality)
    else:
        pil_img.save(output_path, format="PNG", compress_level=1)

    return os.path.isfile(output_path)


def batch_export_frames(
    video_path: str,
    candidates: list[dict],
    output_dir: str,
    prefix: str = "frame",
    img_format: str = "png",
    jpeg_quality: int = 95,
    progress_callback=None,
) -> list[str]:
    """
    Exports all given candidates to output_dir with descriptive filenames.
    Returns list of saved file paths.
    """
    os.makedirs(output_dir, exist_ok=True)
    saved_paths: list[str] = []
    total = len(candidates)

    ext = img_format.lower().lstrip(".")
    if ext not in ("png", "jpg", "jpeg", "webp"):
        ext = "png"

    # Clean prefix for valid filename
    safe_prefix = "".join([c if c.isalnum() or c in " -_" else "_" for c in prefix]).strip()
    if not safe_prefix:
        safe_prefix = "frame"

    for i, c in enumerate(candidates):
        rank = c.get("rank", i + 1)
        t_sec = c.get("timestamp_sec", 0.0)
        m, s = divmod(t_sec, 60.0)
        time_tag = f"{int(m):02d}m{int(s):02d}s"

        filename = f"{safe_prefix}_rank{rank:02d}_{time_tag}.{ext}"
        out_path = os.path.join(output_dir, filename)

        if progress_callback:
            pct = int(100 * (i / max(1, total)))
            progress_callback(f"Exporting image {i + 1}/{total} ({filename})…", pct)

        ok = export_frame(
            video_path,
            t_sec,
            out_path,
            img_format=ext,
            jpeg_quality=jpeg_quality,
        )
        if ok:
            saved_paths.append(out_path)

    if progress_callback:
        progress_callback(f"Exported {len(saved_paths)} images.", 100)

    return saved_paths
