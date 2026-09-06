"""
Frame Extractor & Interesting Image Analyzer for Music Video Pipeline.

Analyzes YouTube videos (long videos and shorts) to identify visually rich,
aesthetic, sharp, and diverse keyframes, and exports them in full resolution.
"""

from __future__ import annotations

import os
import re
import math
import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter


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
    overlay_config: dict | None = None,
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
            if overlay_config and overlay_config.get("enabled", False):
                thumb_pil = render_thumbnail_text_overlay(thumb_pil, overlay_config)

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


# ---------------------------------------------------------------------------
# Font & Typography Overlay Helpers
# ---------------------------------------------------------------------------

DEFAULT_SERIF_FONT = "Baskerville"
DEFAULT_SANS_FONT = "Helvetica"


def get_font_name_for_style(font_style: str = "") -> str:
    """Returns Baskerville for Serif or Helvetica for Sans-Serif."""
    if font_style and "sans" in str(font_style).lower():
        return DEFAULT_SANS_FONT
    return DEFAULT_SERIF_FONT


def resolve_font_path(font_name: str = DEFAULT_SERIF_FONT) -> str:
    """Finds font file on system or returns fallback."""
    if os.path.isfile(font_name):
        return font_name

    search_dirs = [
        "/System/Library/Fonts/Supplemental",
        "/System/Library/Fonts",
        "/Library/Fonts",
        os.path.expanduser("~/Library/Fonts"),
        "/usr/share/fonts",
        "/usr/local/share/fonts",
    ]

    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        for root, _, files in os.walk(d):
            for f in files:
                if not f.lower().endswith((".ttf", ".otf", ".ttc")):
                    continue
                stem = os.path.splitext(f)[0].lower()
                if f.lower() == font_name.lower() or stem == font_name.lower():
                    return os.path.join(root, f)
                if font_name.lower() in stem:
                    return os.path.join(root, f)

    return font_name


def _wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int, draw: ImageDraw.ImageDraw) -> str:
    """Wrap text so each line fits within max_width pixels."""
    paragraphs = text.split("\n")
    wrapped_lines = []
    for para in paragraphs:
        words = para.split()
        if not words:
            wrapped_lines.append("")
            continue
        current_line = words[0]
        for word in words[1:]:
            test_line = current_line + " " + word
            bbox = draw.textbbox((0, 0), test_line, font=font)
            if bbox[2] - bbox[0] <= max_width:
                current_line = test_line
            else:
                wrapped_lines.append(current_line)
                current_line = word
        wrapped_lines.append(current_line)
    return "\n".join(wrapped_lines)


def render_full_video_thumbnail_overlay(
    image: Image.Image,
    title: str = "",
    series: str = "Hypnosonica Presents: Music To Sleep To",
    quote: str = "",
    badge: str = "432Hz Deep Sleep & Relaxation",
    font_name: str = DEFAULT_SERIF_FONT,
    font_scale_pct: float = 100.0,
) -> Image.Image:
    """
    Renders left-aligned, thumbnail-scaled presentation typography onto a 16:9 frame.
    
    Layout:
      - Aligned LEFT with clean margin
      - Scaled large so that it remains prominently legible when displayed as a small thumbnail
      - Subtle dark gradient scrim and multi-pass drop shadow ensures ultra-high contrast on any video background
      - font_scale_pct adjusts all typographic sizes by the given percentage (default 100.0)
    
      Line 1: Track Title (e.g. "Baku's Kindness")
      Line 2: Series (e.g. "Hypnosonica Presents: Music To Sleep To")
      Line 3: "[Bedtime Sleep Quote]"
      Line 4: Audio Badge (e.g. "432Hz Deep Sleep & Relaxation")
    """
    canvas = image.convert("RGBA")
    w, h = canvas.size

    scale_factor = max(0.2, min(3.0, float(font_scale_pct) / 100.0))

    # Font sizes relative to video height, balanced for clear legibility even at small thumbnail card sizes
    # In 3840x2160 (4K): title ~190px, series ~100px, quote ~82px, badge ~74px
    # In 1920x1080 (HD): title ~95px, series ~50px, quote ~41px, badge ~37px
    title_size = max(14, int(round(h * 0.082 * scale_factor)))
    series_size = max(11, int(round(h * 0.044 * scale_factor)))
    quote_size = max(12, int(round(h * 0.052 * scale_factor)))
    badge_size = max(11, int(round(h * 0.044 * scale_factor)))

    font_path = resolve_font_path(font_name)
    try:
        t_font = ImageFont.truetype(font_path, title_size)
    except Exception:
        t_font = ImageFont.load_default()

    try:
        s_font = ImageFont.truetype(font_path, series_size)
    except Exception:
        s_font = ImageFont.load_default()

    try:
        q_font = ImageFont.truetype(font_path, quote_size)
    except Exception:
        q_font = ImageFont.load_default()

    try:
        b_font = ImageFont.truetype(font_path, badge_size)
    except Exception:
        b_font = ImageFont.load_default()

    pad_x = max(18, int(w * 0.065))   # Left margin (~6.5% width)
    max_text_width = int(w * 0.72)    # Reserve right 28% for clean visual breathing room

    draw_meas = ImageDraw.Draw(canvas)

    lines_to_render = []
    badge_lines = []

    if title.strip():
        w_title = _wrap_text(title.strip(), t_font, max_text_width, draw_meas)
        for l in w_title.split("\n"):
            if l.strip():
                bbox = draw_meas.textbbox((0, 0), l, font=t_font)
                lh = bbox[3] - bbox[1]
                lines_to_render.append(("title", l, t_font, lh, (255, 255, 255, 255)))

    if series.strip():
        w_series = _wrap_text(series.strip(), s_font, max_text_width, draw_meas)
        for l in w_series.split("\n"):
            if l.strip():
                bbox = draw_meas.textbbox((0, 0), l, font=s_font)
                lh = bbox[3] - bbox[1]
                lines_to_render.append(("series", l, s_font, lh, (225, 235, 250, 240)))

    if quote.strip():
        q_clean = quote.strip()
        formatted_q = f'"{q_clean}"' if not (q_clean.startswith('"') and q_clean.endswith('"')) else q_clean
        w_quote = _wrap_text(formatted_q, q_font, max_text_width, draw_meas)
        for l in w_quote.split("\n"):
            if l.strip():
                bbox = draw_meas.textbbox((0, 0), l, font=q_font)
                lh = bbox[3] - bbox[1]
                lines_to_render.append(("quote", l, q_font, lh, (245, 240, 225, 235)))

    if badge.strip():
        w_badge = _wrap_text(badge.strip(), b_font, max_text_width, draw_meas)
        for l in w_badge.split("\n"):
            if l.strip():
                bbox = draw_meas.textbbox((0, 0), l, font=b_font)
                lh = bbox[3] - bbox[1]
                lw = bbox[2] - bbox[0]
                badge_lines.append((l, b_font, lh, lw))

    if not lines_to_render and not badge_lines:
        return canvas.convert("RGB")

    # Start ~8% from the top
    start_y = max(18, int(h * 0.08))

    # Add dark gradient scrim on the left side to guarantee contrast
    scrim = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    scrim_draw = ImageDraw.Draw(scrim)
    scrim_width = int(max_text_width + pad_x * 1.8)
    for x in range(min(w, scrim_width)):
        progress = x / scrim_width
        alpha = int(160 * (1.0 - math.sin(progress * math.pi * 0.5)))
        scrim_draw.line([(x, 0), (x, h)], fill=(0, 0, 0, max(0, min(255, alpha))))

    canvas = Image.alpha_composite(canvas, scrim)

    # Soft, diffuse drop shadow layer
    shadow_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    s_draw = ImageDraw.Draw(shadow_layer)

    curr_y = start_y
    for item_type, line_text, font, line_h, _ in lines_to_render:
        s_off = max(3, int(line_h * 0.08))
        s_draw.text((pad_x + s_off, curr_y + s_off), line_text, font=font, fill=(0, 0, 0, 240))
        spacing = int(line_h * 0.25)
        if item_type == "title":
            spacing += int(title_size * 0.20)
        elif item_type == "series":
            spacing += int(series_size * 0.20)
        elif item_type == "quote":
            spacing += int(quote_size * 0.25)
        curr_y += line_h + spacing

    # Pill badge shadow
    pill_padding_x = max(8, int(badge_size * 0.65))
    pill_padding_y = max(4, int(badge_size * 0.32))

    if badge_lines:
        b_shadow_y = curr_y + int(badge_size * 0.25)
        for _, _, b_lh, b_lw in badge_lines:
            s_off = max(3, int(b_lh * 0.08))
            pill_rect_s = [
                pad_x + s_off,
                b_shadow_y - pill_padding_y + s_off,
                pad_x + b_lw + pill_padding_x * 2 + s_off,
                b_shadow_y + b_lh + pill_padding_y + s_off,
            ]
            pill_r_s = (pill_rect_s[3] - pill_rect_s[1]) // 2
            s_draw.rounded_rectangle(pill_rect_s, radius=pill_r_s, fill=(0, 0, 0, 200))
            b_shadow_y += b_lh + pill_padding_y * 2 + int(badge_size * 0.20)

    shadow_blurred = shadow_layer.filter(ImageFilter.GaussianBlur(radius=6))
    canvas = Image.alpha_composite(canvas, shadow_blurred)

    # Primary text with subtle stroke for crisp character separation
    draw = ImageDraw.Draw(canvas)
    curr_y = start_y
    for item_type, line_text, font, line_h, text_color in lines_to_render:
        f_size = getattr(font, "size", line_h)
        st_w = max(1, int(f_size * 0.025))
        draw.text(
            (pad_x, curr_y), line_text, font=font, fill=text_color,
            stroke_width=st_w, stroke_fill=(0, 0, 0, 170),
        )

        spacing = int(line_h * 0.25)
        if item_type == "title":
            spacing += int(title_size * 0.20)
        elif item_type == "series":
            spacing += int(series_size * 0.20)
        elif item_type == "quote":
            spacing += int(quote_size * 0.25)
        curr_y += line_h + spacing

    # Draw Pill Badge
    if badge_lines:
        b_y = curr_y + int(badge_size * 0.25)
        border_w = max(1, int(badge_size * 0.04))
        for b_text, font, b_lh, b_lw in badge_lines:
            pill_rect = [
                pad_x,
                b_y - pill_padding_y,
                pad_x + b_lw + pill_padding_x * 2,
                b_y + b_lh + pill_padding_y,
            ]
            pill_radius = (pill_rect[3] - pill_rect[1]) // 2
            draw.rounded_rectangle(
                pill_rect,
                radius=pill_radius,
                fill=(15, 25, 45, 210),
                outline=(120, 185, 255, 190),
                width=border_w,
            )
            draw.text(
                (pad_x + pill_padding_x, b_y),
                b_text,
                font=font,
                fill=(235, 245, 255, 255),
            )
            b_y += b_lh + pill_padding_y * 2 + int(badge_size * 0.20)

    return canvas.convert("RGB")


def render_short_thumbnail_overlay(
    image: Image.Image,
    text: str,
    font_name: str = DEFAULT_SERIF_FONT,
    position: str = "center",
    font_scale_pct: float = 100.0,
) -> Image.Image:
    """
    Renders the Short overlay text onto every 9:16 frame.
    
    Auto-sizes and wraps text to fit comfortably in the vertical canvas
    with a dark backing shadow / outline for 100% legibility on mobile cards.
    font_scale_pct adjusts font size relative to default balanced scale (default 100.0).
    """
    if not text or not text.strip():
        return image.convert("RGB")

    canvas = image.convert("RGBA")
    w, h = canvas.size

    pad_x = int(w * 0.08)
    pad_y = int(h * 0.12)
    max_w = w - 2 * pad_x
    max_h = int(h * 0.60)

    font_path = resolve_font_path(font_name)
    draw_meas = ImageDraw.Draw(canvas)

    # Binary search optimal baseline font size
    lo, hi = 18, max(24, int(w * 0.14))
    best_font = None
    best_wrapped = text.strip()

    while lo <= hi:
        mid = (lo + hi) // 2
        try:
            f = ImageFont.truetype(font_path, mid)
        except Exception:
            f = ImageFont.load_default()
        wrapped = _wrap_text(text.strip(), f, max_w, draw_meas)
        bbox = draw_meas.multiline_textbbox((0, 0), wrapped, font=f, align="center")
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if tw <= max_w and th <= max_h:
            best_font = f
            best_wrapped = wrapped
            lo = mid + 1
        else:
            hi = mid - 1

    if best_font is None:
        try:
            best_font = ImageFont.truetype(font_path, 28)
        except Exception:
            best_font = ImageFont.load_default()
        best_wrapped = _wrap_text(text.strip(), best_font, max_w, draw_meas)

    # Apply font percentage scaling if requested
    scale_factor = max(0.2, min(3.0, float(font_scale_pct) / 100.0))
    if abs(scale_factor - 1.0) > 0.01:
        cur_size = getattr(best_font, "size", 28)
        target_size = max(12, int(round(cur_size * scale_factor)))
        try:
            best_font = ImageFont.truetype(font_path, target_size)
        except Exception:
            pass
        best_wrapped = _wrap_text(text.strip(), best_font, max_w, draw_meas)

    bbox = draw_meas.multiline_textbbox((0, 0), best_wrapped, font=best_font, align="center")
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]

    x = (w - tw) // 2
    if position == "top":
        y = pad_y
    elif position == "bottom":
        y = h - pad_y - th
    else:  # "center"
        y = (h - th) // 2

    # Scrim behind text for contrast
    scrim = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    scrim_draw = ImageDraw.Draw(scrim)
    scrim_pad_y = int(th * 0.35)
    scrim_top = max(0, y - scrim_pad_y)
    scrim_bot = min(h, y + th + scrim_pad_y)

    for sy in range(scrim_top, scrim_bot):
        rel = (sy - scrim_top) / max(1, (scrim_bot - scrim_top))
        alpha = int(140 * math.sin(rel * math.pi))
        scrim_draw.line([(0, sy), (w, sy)], fill=(0, 0, 0, alpha))

    canvas = Image.alpha_composite(canvas, scrim)

    # Soft, diffuse drop shadow layer
    shadow_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    s_draw = ImageDraw.Draw(shadow_layer)
    f_size = getattr(best_font, "size", 40)
    s_off = max(4, int(f_size * 0.05))
    s_draw.multiline_text(
        (x + s_off, y + s_off), best_wrapped, font=best_font,
        fill=(0, 0, 0, 245), align="center",
    )
    blur_rad = max(4, int(f_size * 0.08))
    shadow_blurred = shadow_layer.filter(ImageFilter.GaussianBlur(radius=blur_rad))
    canvas = Image.alpha_composite(canvas, shadow_blurred)

    # Main text with subtle stroke for crisp character separation
    draw = ImageDraw.Draw(canvas)
    st_w = max(1, int(f_size * 0.02))
    draw.multiline_text(
        (x, y), best_wrapped, font=best_font,
        fill=(255, 255, 255, 255),
        stroke_width=st_w,
        stroke_fill=(0, 0, 0, 160),
        align="center",
    )

    return canvas.convert("RGB")


def render_thumbnail_text_overlay(
    image: Image.Image,
    overlay_config: dict | None = None,
) -> Image.Image:
    """
    Applies the appropriate thumbnail text overlay according to overlay_config:
    
    overlay_config format:
      {
        "enabled": bool,
        "type": "full" | "short",
        # For full video (16:9):
        "title": str,
        "series": str,
        "quote": str,
        "badge": str,
        # For short (9:16):
        "short_text": str,
        "font_name": str (optional),
        "font_scale_pct": float (optional, default 100.0),
      }
    """
    if not overlay_config or not overlay_config.get("enabled", False):
        return image

    w, h = image.size
    overlay_type = overlay_config.get("type")
    if not overlay_type:
        overlay_type = "short" if h > w else "full"

    font_style = overlay_config.get("font_style", "")
    if font_style:
        font_name = get_font_name_for_style(font_style)
    else:
        font_name = overlay_config.get("font_name", DEFAULT_SERIF_FONT)
    try:
        font_scale_pct = float(overlay_config.get("font_scale_pct", 100.0))
    except (ValueError, TypeError):
        font_scale_pct = 100.0

    if overlay_type == "short":
        short_text = overlay_config.get("short_text", "")
        return render_short_thumbnail_overlay(
            image, short_text, font_name=font_name, font_scale_pct=font_scale_pct
        )
    else:
        title = overlay_config.get("title", "")
        series = overlay_config.get("series", "Hypnosonica Presents: Music To Sleep To")
        quote = overlay_config.get("quote", "")
        badge = overlay_config.get("badge", "432Hz Deep Sleep & Relaxation")
        return render_full_video_thumbnail_overlay(
            image,
            title=title,
            series=series,
            quote=quote,
            badge=badge,
            font_name=font_name,
            font_scale_pct=font_scale_pct,
        )


# ---------------------------------------------------------------------------
# Frame Extraction & Export
# ---------------------------------------------------------------------------

def extract_thumbnail_image(
    video_path: str,
    timestamp_sec: float,
    max_dim: int = 360,
    overlay_config: dict | None = None,
) -> Image.Image | None:
    """
    Extracts a frame and scales it to a PIL Image suitable for UI preview.
    Preserves aspect ratio (for both 16:9 widescreen and 9:16 vertical shorts).
    Optionally burns the thumbnail text overlay onto the image.
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

    if overlay_config and overlay_config.get("enabled", False):
        # Render typography on normalized high-resolution canvas first so title, quote, and badge scale smoothly
        if max(w, h) > 1920:
            scale_preview = 1920.0 / max(w, h)
            preview_w = max(1, int(w * scale_preview))
            preview_h = max(1, int(h * scale_preview))
            high_res = Image.fromarray(cv2.resize(rgb, (preview_w, preview_h), interpolation=cv2.INTER_AREA))
        else:
            high_res = Image.fromarray(rgb)

        rendered_high_res = render_thumbnail_text_overlay(high_res, overlay_config)
        pil_img = rendered_high_res.resize((nw, nh), Image.Resampling.LANCZOS)
    else:
        small = cv2.resize(rgb, (nw, nh), interpolation=cv2.INTER_AREA)
        pil_img = Image.fromarray(small)

    return pil_img


def export_frame(
    video_path: str,
    timestamp_sec: float,
    output_path: str,
    img_format: str = "png",
    jpeg_quality: int = 95,
    overlay_config: dict | None = None,
) -> bool:
    """
    Extracts the full-resolution frame at timestamp_sec and saves it to output_path.
    Applies text overlay if overlay_config['enabled'] is True.
    Supported formats: 'png', 'jpg' / 'jpeg', 'webp'.
    """
    rgb = extract_full_frame(video_path, timestamp_sec)
    if rgb is None:
        return False

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    pil_img = Image.fromarray(rgb)

    if overlay_config and overlay_config.get("enabled", False):
        pil_img = render_thumbnail_text_overlay(pil_img, overlay_config)

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
    overlay_config: dict | None = None,
    progress_callback=None,
) -> list[str]:
    """
    Exports all given candidates to output_dir with descriptive filenames.
    Applies text overlay to every frame if overlay_config is provided.
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
            overlay_config=overlay_config,
        )
        if ok:
            saved_paths.append(out_path)

    if progress_callback:
        progress_callback(f"Exported {len(saved_paths)} images.", 100)

    return saved_paths

