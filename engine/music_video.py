#!/usr/bin/env python3
"""
Fractal Flame Music Video Generator – Space Nebula Style

Renders Julia-set orbit traps with fractal-flame-style additive glow
on a deep-space background.  Multiple trap shapes (concentric circles,
axis cross, diagonal lattice) accumulate as emitted light, producing
luminescent nebula ribbons.  A deterministic starfield completes the
space atmosphere.

Usage:
    python music_video.py --audio music.mp3 --output video.mp4
"""

import argparse
import json
import math
import os
import random
import sys

import numpy as np
import librosa
from moviepy import ImageClip, AudioFileClip, concatenate_videoclips, CompositeAudioClip
import cv2


# ---------------------------------------------------------------------------
# Julia c-value presets – chosen for rich, layered orbit-trap shapes
# ---------------------------------------------------------------------------
JULIA_PRESETS = [
    # Original 16
    (-0.7269,  0.1889),
    (-0.8,     0.156),
    ( 0.285,   0.01),
    (-0.4,     0.6),
    (-0.54,    0.54),
    ( 0.37,    0.1),
    (-0.12,   -0.77),
    ( 0.28,    0.008),
    (-0.62,    0.0),
    (-0.1,     0.651),
    ( 0.0,    -0.8),
    (-0.75,    0.0),
    (-0.835,  -0.2321),
    (-0.70176,-0.3842),
    ( 0.355,   0.355),
    ( 0.32,    0.043),
    # Extended set
    (-0.5251,  0.5255),   # dense spiral arms
    ( 0.285,   0.535),    # branching filaments
    (-0.7,     0.27015),  # classic dendrite variant
    ( 0.45,    0.1428),   # tight concentric rings
    (-0.624,   0.435),    # open lattice
    (-0.75,    0.11),     # Douady rabbit
    ( 0.3,    -0.5),      # swept spirals
    (-0.2,     0.65),     # broad flame wings
    ( 0.35,   -0.3),      # swept arc
    (-0.5,     0.5),      # diagonal cross structure
    (-0.391,  -0.587),    # twisted rings
    ( 0.21,    0.52),     # soft cloud clusters
    (-0.6,     0.4),      # interlaced arcs
    (-0.11,    0.6557),   # airplane julia
    (-0.7,    -0.3),      # asymmetric spiral
    ( 0.37,   -0.2),      # open feathers
    (-0.5,    -0.56),     # dense tangle
    ( 0.28,   -0.45),     # ribbon lattice
    (-0.4,    -0.59),     # braided arms
    ( 0.0,     0.67),     # symmetric flame column
    (-0.9,     0.0),      # Cantor dust spine
    (-0.1,    -0.8),      # swept Douady variant
    ( 0.255,   0.511),    # overlapping rings
    (-0.74543, 0.11301),  # fine filament web
    ( 0.4,     0.2),      # asymmetric petals
    (-0.8,    -0.175),    # deep spiral sink
    ( 0.3,     0.5),      # open star burst
    (-0.6180,  0.0),      # golden-ratio spine
]


# ---------------------------------------------------------------------------
# Space / nebula colour palettes
# Each palette has a near-black background and 5 emissive colours.
# Colours are blended with orbit-angle data to get organic variation.
# ---------------------------------------------------------------------------
PALETTES = [
    # Deep space: electric blue + violet + cyan + star-white + deep purple
    {
        "bg": (2, 2, 10),
        "colors": [
            (0,   60, 200),   # deep blue
            (110,  0, 230),   # violet
            (0,  210, 250),   # cyan
            (255, 255, 255),  # star white
            (55,   0, 130),   # deep purple
        ],
    },
    # Crimson nebula: deep red + magenta + amber + warm white + dark wine
    {
        "bg": (7, 1, 3),
        "colors": [
            (200,  10,  25),  # deep crimson
            (240,   0, 130),  # magenta
            (215, 130,   0),  # amber
            (255, 245, 210),  # warm white
            (110,   0,  50),  # dark wine
        ],
    },
    # Aurora: teal + emerald + violet + ice blue + dark ocean
    {
        "bg": (1, 5, 9),
        "colors": [
            (0,  145, 165),   # deep teal
            (0,  225, 115),   # emerald
            (120,  0, 210),   # violet
            (200, 235, 255),  # ice blue
            (0,   65, 105),   # dark ocean
        ],
    },
    # Supernova: hot yellow + orange + deep red + dark magenta + star white
    {
        "bg": (4, 2, 6),
        "colors": [
            (255, 225, 110),  # hot yellow
            (255, 100,  20),  # orange
            (195,  20,   0),  # deep red
            (110,   0,  90),  # dark magenta
            (255, 255, 230),  # star white
        ],
    },
    # Cosmic dust: steel blue + dusty purple + periwinkle + pale gold + navy
    {
        "bg": (2, 2, 8),
        "colors": [
            (25,  35, 110),   # dark navy
            (90,  20, 125),   # dusty purple
            (55, 105, 210),   # steel blue
            (205, 175,  70),  # pale gold
            (145, 160, 225),  # periwinkle
        ],
    },
]


# ---------------------------------------------------------------------------
# Audio analysis
# ---------------------------------------------------------------------------

def analyze_audio(audio_path: str):
    """Extract smoothed audio feature envelopes."""
    print(f"Analyzing audio: {audio_path}")
    y, sr = librosa.load(audio_path, sr=None)
    duration = librosa.get_duration(y=y, sr=sr)

    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
    beat_times = librosa.frames_to_time(beat_frames, sr=sr)
    print(f"  Tempo: {float(np.atleast_1d(tempo)[0]):.1f} BPM, "
          f"{len(beat_times)} beats, duration: {duration:.1f}s")

    hop = 512
    rms      = librosa.feature.rms(y=y, hop_length=hop)[0]
    centroid = librosa.feature.spectral_centroid(y=y, sr=sr, hop_length=hop)[0]
    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    chroma    = librosa.feature.chroma_stft(y=y, sr=sr, hop_length=hop)
    chroma_peak = np.argmax(chroma, axis=0).astype(np.float32) / 12.0

    n     = len(rms)
    times = librosa.frames_to_time(np.arange(n), sr=sr, hop_length=hop)

    def _norm(x):
        mn, mx = x.min(), x.max()
        return (x - mn) / (mx - mn + 1e-10)

    rms_n      = _norm(rms)
    centroid_n = _norm(centroid)
    onset_n    = _norm(onset_env[:n])

    # Beat envelope – moderate decay for rhythmic pulse
    beat_env = np.zeros(n, dtype=np.float32)
    for bt in beat_times:
        idx = np.argmin(np.abs(times - bt))
        if idx < n:
            beat_env[idx] = 1.0
    for i in range(1, n):
        beat_env[i] = max(beat_env[i], beat_env[i - 1] * 0.93)

    kern           = np.ones(40) / 40
    rms_smooth      = np.convolve(rms_n,      kern, mode="same")
    centroid_smooth = np.convolve(centroid_n, kern, mode="same")
    onset_smooth    = np.convolve(onset_n,    kern, mode="same")

    return duration, {
        "times":    times,
        "rms":      rms_smooth,
        "centroid": centroid_smooth,
        "onset":    onset_smooth,
        "beat_env": beat_env,
        "chroma":   chroma_peak,
    }


def features_at(af: dict, t: float) -> dict:
    """Look up audio features at time t."""
    times = af["times"]
    idx   = np.searchsorted(times, t, side="right") - 1
    idx   = max(0, min(idx, len(times) - 1))
    return {k: float(af[k][idx]) for k in af if k != "times"}


# ---------------------------------------------------------------------------
# Interpolation
# ---------------------------------------------------------------------------

def smoothstep(t):
    t = np.clip(t, 0.0, 1.0)
    return t * t * t * (t * (t * 6 - 15) + 10)


def interp_c(a, b, t):
    s = float(smoothstep(np.asarray(t)))
    return (a[0] * (1 - s) + b[0] * s,
            a[1] * (1 - s) + b[1] * s)


# ---------------------------------------------------------------------------
# Fractal flame-style renderer  (dark space + additive glow)
# ---------------------------------------------------------------------------

def render_fractal_flame(
    width, height,
    cx, cy,
    zoom, rotation,
    max_iter,
    trap_radius,
    palette_t, palette_idx,
    energy, beat,
) -> np.ndarray:
    """
    Render a Julia set using orbit traps with fractal-flame-style additive
    glow composited on a deep-space background.

    Six trap shapes are used:
      • Three concentric circle traps  (r1 primary, r2 inner, r3 outer)
      • One hot-core micro-trap        (r4 very small)
      • Axis-cross trap                (thin bright filaments)
      • Diagonal-lattice trap          (45° secondary structure)

    Minimum trap distances are accumulated per pixel across all iterations.
    Each trap layer is rendered at multiple sharpness levels (thick diffuse
    glow + medium filaments + fine edge detail) and composited additively
    so the image builds up like emitted light on a dark background.

    Angle at closest orbit approach drives per-pixel colour mixing, creating
    the organic colour banding characteristic of fractal flame images.
    """
    aspect = width / height
    re = np.linspace(-zoom * aspect, zoom * aspect, width,  dtype=np.float64)
    im = np.linspace(-zoom,          zoom,          height, dtype=np.float64)
    re_g, im_g = np.meshgrid(re, im)

    cos_r, sin_r = math.cos(rotation), math.sin(rotation)
    zr = re_g * cos_r - im_g * sin_r
    zi = re_g * sin_r + im_g * cos_r

    # ── Accumulators ──────────────────────────────────────────────────────
    INF = 1e10
    min_d1    = np.full((height, width), INF)   # primary circle
    min_d2    = np.full((height, width), INF)   # inner circle
    min_d3    = np.full((height, width), INF)   # outer circle
    min_d4    = np.full((height, width), INF)   # hot-core micro-trap
    min_cross = np.full((height, width), INF)   # axis cross
    min_diag  = np.full((height, width), INF)   # 45° diagonal

    # Orbit angle at closest approach (per trap – drives colour variation)
    ang1 = np.zeros((height, width))
    ang2 = np.zeros((height, width))
    # Normalised orbit magnitude at closest cross approach (for colour mix)
    mag_cross = np.zeros((height, width))

    mask = np.ones((height, width), dtype=bool)

    r1 = trap_radius
    r2 = trap_radius * 0.40
    r3 = trap_radius * 1.70
    r4 = trap_radius * 0.12   # tiny hot-core ring

    # ── Julia iteration ───────────────────────────────────────────────────
    for _ in range(max_iter):
        zr2  = zr * zr
        zi2  = zi * zi
        mag2 = zr2 + zi2

        new_escaped   = mask & (mag2 > 100.0)
        mask[new_escaped] = False
        zr[~mask] = 0.0
        zi[~mask] = 0.0

        if not mask.any():
            break

        mag = np.sqrt(np.maximum(mag2, 1e-12))

        d1    = np.abs(mag - r1)
        d2    = np.abs(mag - r2)
        d3    = np.abs(mag - r3)
        d4    = np.abs(mag - r4)
        d_cross = np.minimum(np.abs(zi), np.abs(zr))
        d_diag  = np.minimum(np.abs(zr - zi), np.abs(zr + zi)) * 0.7071

        u1 = mask & (d1 < min_d1)
        min_d1[u1] = d1[u1]
        ang1[u1]   = np.arctan2(zi[u1], zr[u1])

        u2 = mask & (d2 < min_d2)
        min_d2[u2] = d2[u2]
        ang2[u2]   = np.arctan2(zi[u2], zr[u2])

        u3 = mask & (d3 < min_d3)
        min_d3[u3] = d3[u3]

        u4 = mask & (d4 < min_d4)
        min_d4[u4] = d4[u4]

        uc = mask & (d_cross < min_cross)
        min_cross[uc]  = d_cross[uc]
        mag_cross[uc]  = mag[uc]

        ud = mask & (d_diag < min_diag)
        min_diag[ud] = d_diag[ud]

        zi_new = 2.0 * zr * zi + cy
        zr     = zr2 - zi2 + cx
        zi     = zi_new

    # ── Palette blend ─────────────────────────────────────────────────────
    pal_a = PALETTES[palette_idx % len(PALETTES)]
    pal_b = PALETTES[(palette_idx + 1) % len(PALETTES)]
    pt    = float(smoothstep(np.asarray([palette_t]))[0])

    def blend_col(idx):
        ca = np.array(pal_a["colors"][idx % 5], dtype=float) / 255.0
        cb = np.array(pal_b["colors"][idx % 5], dtype=float) / 255.0
        return ca * (1 - pt) + cb * pt

    C = [blend_col(i) for i in range(5)]
    # C[0] deep hue · C[1] secondary · C[2] accent · C[3] hot/bright · C[4] shadow

    # Background
    bg_a = np.array(pal_a["bg"], dtype=float) / 255.0
    bg_b = np.array(pal_b["bg"], dtype=float) / 255.0
    bg   = bg_a * (1 - pt) + bg_b * pt

    # ── Glow layers from min-distance fields ─────────────────────────────
    sharp = 18.0 + 10.0 * energy   # audio energy sharpens filaments

    # Outer diffuse cloud – broad gentle glow (deepest space haze)
    g3_thick = np.exp(-min_d3 * (sharp * 0.15))
    g3_mid   = np.exp(-min_d3 * (sharp * 0.55))

    # Primary circle – main nebula ribbon
    g1_thick = np.exp(-min_d1 * (sharp * 0.25))
    g1_mid   = np.exp(-min_d1 *  sharp)
    g1_fine  = np.exp(-min_d1 * (sharp * 2.5))

    # Inner circle – accent / colour accent layer
    g2_thick = np.exp(-min_d2 * (sharp * 0.40))
    g2_mid   = np.exp(-min_d2 * (sharp * 1.5))
    g2_fine  = np.exp(-min_d2 * (sharp * 3.5))

    # Hot core – tiny bright star-like ring
    g4_hot   = np.exp(-min_d4 * (sharp * 6.0))

    # Cross trap – thin bright filaments along coordinate axes
    gc_mid  = np.exp(-min_cross * (sharp * 1.2))
    gc_fine = np.exp(-min_cross * (sharp * 3.2))

    # Diagonal trap – secondary lattice structure
    gd_mid  = np.exp(-min_diag  * (sharp * 1.0))

    # ── Per-pixel colour mixing (angle → colour variation) ────────────────
    mix1 = (ang1 / (2.0 * math.pi) + 0.5)          # 0-1, drives C[0]↔C[1]
    mix2 = (ang2 / (2.0 * math.pi) + 0.5)          # 0-1, drives C[2]↔C[3]
    mixc = np.clip(mag_cross / max(r1 * 2, 1e-6), 0.0, 1.0)  # 0-1

    # Per-pixel colour arrays for angle-blended layers
    col1_r = C[0][0] * (1 - mix1) + C[1][0] * mix1
    col1_g = C[0][1] * (1 - mix1) + C[1][1] * mix1
    col1_b = C[0][2] * (1 - mix1) + C[1][2] * mix1

    col2_r = C[2][0] * (1 - mix2) + C[3][0] * mix2
    col2_g = C[2][1] * (1 - mix2) + C[3][1] * mix2
    col2_b = C[2][2] * (1 - mix2) + C[3][2] * mix2

    colc_r = C[2][0] * (1 - mixc) + C[3][0] * mixc
    colc_g = C[2][1] * (1 - mixc) + C[3][1] * mixc
    colc_b = C[2][2] * (1 - mixc) + C[3][2] * mixc

    # Audio brightness modulator
    bright = 1.0 + 0.45 * energy + 0.30 * beat

    # ── Additive compositing ──────────────────────────────────────────────
    acc_r = np.full((height, width), bg[0])
    acc_g = np.full((height, width), bg[1])
    acc_b = np.full((height, width), bg[2])

    # Outer haze (C[4] shadow tone)
    acc_r += g3_thick * C[4][0] * 0.30 * bright
    acc_g += g3_thick * C[4][1] * 0.30 * bright
    acc_b += g3_thick * C[4][2] * 0.30 * bright

    acc_r += g3_mid * C[0][0] * 0.40 * bright
    acc_g += g3_mid * C[0][1] * 0.40 * bright
    acc_b += g3_mid * C[0][2] * 0.40 * bright

    # Primary ribbon – thick + medium + fine edge
    acc_r += g1_thick * col1_r * 0.35 * bright
    acc_g += g1_thick * col1_g * 0.35 * bright
    acc_b += g1_thick * col1_b * 0.35 * bright

    acc_r += g1_mid * col1_r * 0.85 * bright
    acc_g += g1_mid * col1_g * 0.85 * bright
    acc_b += g1_mid * col1_b * 0.85 * bright

    acc_r += g1_fine * C[3][0] * 0.55 * bright   # bright edge highlight
    acc_g += g1_fine * C[3][1] * 0.55 * bright
    acc_b += g1_fine * C[3][2] * 0.55 * bright

    # Inner accent ribbon
    acc_r += g2_thick * col2_r * 0.25 * bright
    acc_g += g2_thick * col2_g * 0.25 * bright
    acc_b += g2_thick * col2_b * 0.25 * bright

    acc_r += g2_mid * col2_r * 0.65 * bright
    acc_g += g2_mid * col2_g * 0.65 * bright
    acc_b += g2_mid * col2_b * 0.65 * bright

    acc_r += g2_fine * C[3][0] * 0.75 * bright   # hot core accent
    acc_g += g2_fine * C[3][1] * 0.75 * bright
    acc_b += g2_fine * C[3][2] * 0.75 * bright

    # Micro hot-core ring (star-like bright point)
    acc_r += g4_hot * C[3][0] * 1.20 * bright
    acc_g += g4_hot * C[3][1] * 1.20 * bright
    acc_b += g4_hot * C[3][2] * 1.20 * bright

    # Axis-cross filaments
    acc_r += gc_mid  * colc_r * 0.50 * bright
    acc_g += gc_mid  * colc_g * 0.50 * bright
    acc_b += gc_mid  * colc_b * 0.50 * bright

    acc_r += gc_fine * C[3][0] * 0.85 * bright   # bright axis threads
    acc_g += gc_fine * C[3][1] * 0.85 * bright
    acc_b += gc_fine * C[3][2] * 0.85 * bright

    # Diagonal lattice
    acc_r += gd_mid * C[1][0] * 0.40 * bright
    acc_g += gd_mid * C[1][1] * 0.40 * bright
    acc_b += gd_mid * C[1][2] * 0.40 * bright

    # Beat flash: brief cool-white pulse on strong beats
    if beat > 0.65:
        flash = (beat - 0.65) / 0.35   # 0-1
        pulse = (g1_fine + g2_fine + gc_fine) * flash * 0.25
        acc_r += pulse * 0.85
        acc_g += pulse * 0.90
        acc_b += pulse * 1.00

    # ── Tone mapping + gamma ──────────────────────────────────────────────
    # Filmic Reinhard: compresses highlights, keeps dark areas dark
    acc_r = acc_r / (acc_r + 1.0)
    acc_g = acc_g / (acc_g + 1.0)
    acc_b = acc_b / (acc_b + 1.0)

    # Gamma correction: reveals fine dim filaments
    gamma = 0.45
    acc_r = np.power(np.maximum(acc_r, 0.0), gamma)
    acc_g = np.power(np.maximum(acc_g, 0.0), gamma)
    acc_b = np.power(np.maximum(acc_b, 0.0), gamma)

    # ── Starfield overlay ─────────────────────────────────────────────────
    rng           = np.random.default_rng(seed=77321)
    star_thresh   = rng.random((height, width))
    star_intensity = rng.random((height, width))
    is_star = star_thresh > 0.9984          # ~0.16 % of pixels
    s_int   = star_intensity * (0.55 + 0.45 * beat)
    acc_r   = np.where(is_star, np.maximum(acc_r, s_int * 0.82), acc_r)
    acc_g   = np.where(is_star, np.maximum(acc_g, s_int * 0.88), acc_g)
    acc_b   = np.where(is_star, np.maximum(acc_b, s_int * 1.00), acc_b)

    img = np.stack([acc_r, acc_g, acc_b], axis=2)
    return np.clip(img * 255, 0, 255).astype(np.uint8)


# ---------------------------------------------------------------------------
# Single image generation
# ---------------------------------------------------------------------------

def render_fractal_image(
    output_path: str,
    width: int = 3840,
    height: int = 2160,
    preset_index: int | None = None,
    cx: float | None = None,
    cy: float | None = None,
    randomize: bool = False,
    palette_index: int | None = None,
    zoom: float = 1.5,
    rotation: float = 0.0,
    max_iter: int = 80,
    trap_radius: float = 0.85,
    energy: float = 0.3,
    beat: float = 0.0,
    brightness: float = 1.0,
):
    """
    Render a single fractal flame image and save as PNG.

    Priority for c-value selection:
      1. Explicit --cx / --cy values
      2. --preset N (index into JULIA_PRESETS)
      3. --random (pick a random preset)
      4. Default: preset 0
    """
    # Determine c-value
    if cx is not None and cy is not None:
        c = (cx, cy)
        source = f"custom c=({cx}, {cy})"
    elif randomize:
        c = random.choice(JULIA_PRESETS)
        idx = JULIA_PRESETS.index(c)
        source = f"random preset #{idx} c=({c[0]}, {c[1]})"
    elif preset_index is not None:
        idx = preset_index % len(JULIA_PRESETS)
        c = JULIA_PRESETS[idx]
        source = f"preset #{idx} c=({c[0]}, {c[1]})"
    else:
        c = JULIA_PRESETS[0]
        source = f"preset #0 c=({c[0]}, {c[1]})"

    # Determine palette
    if palette_index is not None:
        pal_idx = palette_index % len(PALETTES)
    elif randomize:
        pal_idx = random.randint(0, len(PALETTES) - 1)
    else:
        pal_idx = 0

    print(f"Rendering fractal image: {width}x{height}")
    print(f"  Source:  {source}")
    print(f"  Palette: {pal_idx}  Zoom: {zoom}  Rotation: {rotation:.2f}")
    print(f"  Iter: {max_iter}  Trap radius: {trap_radius}  Energy: {energy}")
    if brightness != 1.0:
        print(f"  Brightness: {brightness:.2f}x")

    frame = render_fractal_flame(
        width, height,
        c[0], c[1],
        zoom, rotation,
        max_iter,
        trap_radius,
        palette_t=0.0,
        palette_idx=pal_idx,
        energy=energy,
        beat=beat,
    )

    # Apply brightness adjustment
    if brightness != 1.0:
        frame = np.clip(frame.astype(np.float32) * brightness, 0, 255).astype(np.uint8)

    # Convert RGB → BGR for OpenCV
    bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
    cv2.imwrite(output_path, bgr)

    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    print(f"\nDone! {output_path} ({size_mb:.1f} MB)")


# ---------------------------------------------------------------------------
# Main video generation
# ---------------------------------------------------------------------------

def create_fractal_video(
    audio_path: str,
    output_path: str,
    width: int  = 1920,
    height: int = 1080,
    fps: int    = 30,
    num_sections: int = 0,
    loop_close_secs: float = 0.0,
    render_secs: float = 0.0,
    beat_strength: float = 1.0,
    beat_dynamics: float = 0.0,
    randomize: bool = False,
    preset_file: str = "",
    watermark: str = "music_to_sleep_to_profile.png",
    negative_logo: bool = False,
):
    """
    loop_close_secs  – if > 0, append this many silent seconds after the
                       audio ends where the fractal smoothly transitions
                       back to the opening shape, completing the loop.
    render_secs      – if > 0, stop rendering after this many seconds
                       (useful for quick previews; 0 = full video).
    beat_strength    – multiplier on all beat-driven effects (zoom retraction,
                       trap radius breathing, glow brightness, beat flash).
                       1.0 = default, 0.0 = no beat response, 2.0 = intense.
    beat_dynamics    – how much the local audio energy gates the beat pulse.
                       0.0 = every beat hits equally hard (flat);
                       1.0 = beat strength fully tracks the current RMS level
                             so quiet passages pulse gently and loud passages
                             pulse hard. Values in between give partial gating.
    """
    duration, af = analyze_audio(audio_path)
    total_frames = int(duration * fps)

    # ── Preset sequence: load from file, randomize, or evenly sample ────────
    preset_loaded = False
    if preset_file and os.path.isfile(preset_file) and not randomize:
        with open(preset_file) as f:
            data = json.load(f)
        presets = [tuple(p) for p in data["presets"]]
        num_sections = len(presets)
        print(f"  Loaded {num_sections} presets from {preset_file}")
        preset_loaded = True

    if not preset_loaded:
        if num_sections <= 0:
            num_sections = min(len(JULIA_PRESETS), max(3, int(duration / 15)))
        pool = list(JULIA_PRESETS)
        if randomize:
            random.shuffle(pool)
            presets = pool[:num_sections]
        else:
            indices = np.linspace(0, len(pool) - 1, num_sections, dtype=int).tolist()
            presets = [pool[i] for i in indices]

        if preset_file:
            with open(preset_file, "w") as f:
                json.dump({"presets": [list(p) for p in presets]}, f, indent=2)
            action = "Randomized" if randomize else "Sampled"
            print(f"  {action} {num_sections} presets → saved to {preset_file}")

    section_dur = duration / num_sections

    # Extra silent frames that close the loop back to the opening shape
    close_frames = int(loop_close_secs * fps) if loop_close_secs > 0 else 0

    # How many frames to actually render (0 = everything)
    if render_secs > 0:
        render_limit = min(int(render_secs * fps), total_frames + close_frames)
    else:
        render_limit = total_frames + close_frames

    render_dur = render_limit / fps
    print(f"\nGenerating {render_limit} frames ({render_dur:.1f}s) at {fps} fps")
    print(f"  {num_sections} fractal sections, "
          f"~{section_dur:.1f}s each, resolution {width}×{height}")
    if close_frames:
        close_actual = min(close_frames, max(0, render_limit - total_frames))
        print(f"  Loop-close outro: {close_actual / fps:.1f}s "
              f"({close_actual} frames, silent)")
    if render_secs > 0:
        print(f"  Preview mode: rendering first {render_dur:.1f}s only")

    # Load watermark if specified and exists
    def _resolve_wm(wm_path: str) -> str:
        if not wm_path or wm_path.lower() in ("none", ""):
            return ""
        if os.path.exists(wm_path):
            return wm_path
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for c in (os.path.join(root, "assets", wm_path), os.path.join(root, wm_path), os.path.join(root, "assets", os.path.basename(wm_path))):
            if os.path.exists(c):
                return c
        return wm_path

    watermark_data = None
    resolved_wm = _resolve_wm(watermark)
    if resolved_wm and resolved_wm.lower() not in ("none", "") and os.path.exists(resolved_wm):
        try:
            logo = cv2.imread(resolved_wm, cv2.IMREAD_UNCHANGED)
            if logo is not None and len(logo.shape) == 3 and logo.shape[2] == 4:
                # Resize logo to fit in bottom right corner (12% of video height)
                w_h = int(height * 0.12)
                aspect_ratio = logo.shape[1] / logo.shape[0]
                w_w = int(w_h * aspect_ratio)
                logo = cv2.resize(logo, (w_w, w_h))
                
                logo_rgb = cv2.cvtColor(logo[:, :, :3], cv2.COLOR_BGR2RGB)
                if negative_logo:
                    logo_rgb = 255 - logo_rgb
                logo_alpha = (logo[:, :, 3] / 255.0) * 0.4  # 40% opacity
                
                # Bottom-right position with 2% padding
                pad_x = int(width * 0.02)
                pad_y = int(height * 0.02)
                y1 = height - pad_y - w_h
                y2 = height - pad_y
                x1 = width - pad_x - w_w
                x2 = width - pad_x
                
                watermark_data = (logo_rgb, logo_alpha, y1, y2, x1, x2)
                print(f"  Watermark loaded: {watermark} ({w_w}x{w_h} at bottom-right)")
        except Exception as e:
            print(f"  Warning: could not load watermark: {e}")

    rotation_acc = 0.0
    all_frames   = []

    print("\nRendering fractal frames...")

    for fi in range(render_limit):
        t          = fi / fps
        in_close   = close_frames > 0 and fi >= total_frames

        if in_close:
            # ── Loop-close outro: smoothly return to the opening shape ────
            close_progress = (fi - total_frames) / close_frames  # 0 → 1
            cx, cy = interp_c(presets[-1], presets[0], close_progress)
            # No audio during silence – settle energy/beat to zero
            energy = 0.0
            beat   = 0.0
            # Gentle continuous breathing, no beat-driven jumps
            zoom        = 1.40 + 0.20 * math.sin(t * 0.12)
            zoom        = max(0.50, zoom)
            trap_radius = 0.75 + 0.20 * math.sin(t * 0.18)
            max_iter    = 60
            # Hold the palette that the last audio frame landed on
            pal_f       = duration / (section_dur * 2.5)
            palette_idx = int(pal_f) % len(PALETTES)
            palette_t   = pal_f - int(pal_f)
        else:
            # ── Normal audio-driven frame ─────────────────────────────────
            section_f        = t / section_dur
            section_idx      = min(int(section_f), num_sections - 1)
            section_progress = section_f - int(section_f)
            next_idx         = min(section_idx + 1, num_sections - 1)

            feat   = features_at(af, t)
            energy = feat["rms"]
            bright = feat["centroid"]
            beat   = feat["beat_env"]
            onset  = feat["onset"]

            # beat_dynamics gates pulse height by current energy level:
            #   0.0 → every beat hits equally (flat)
            #   1.0 → beat strength fully tracks RMS (quiet = gentle pulse)
            dynamic_scale = 1.0 - beat_dynamics + beat_dynamics * energy
            effective_beat = beat * beat_strength * dynamic_scale

            cx, cy = interp_c(presets[section_idx], presets[next_idx],
                              section_progress)
            cx += 0.010 * math.sin(t * 0.9  + onset * 0.5) * (0.4 + energy * 0.4)
            cy += 0.010 * math.cos(t * 0.70 + onset * 0.3) * (0.4 + bright  * 0.4)

            zoom        = 1.40 + 0.35 * math.sin(t * 0.12) + 0.12 * energy - 0.08 * effective_beat
            zoom        = max(0.50, zoom)
            trap_radius = 0.75 + 0.35 * math.sin(t * 0.18) + 0.18 * effective_beat
            max_iter    = int(55 + 35 * energy + 10 * effective_beat)

            pal_f       = t / (section_dur * 2.5)
            palette_idx = int(pal_f) % len(PALETTES)
            palette_t   = pal_f - int(pal_f)

        # ── Rotation always advances smoothly ─────────────────────────────
        onset_val     = 0.0 if in_close else feat["onset"]
        rotation_acc += (0.05 + 0.03 * onset_val) / fps
        rotation      = rotation_acc

        frame = render_fractal_flame(
            width, height,
            cx, cy,
            zoom, rotation,
            max_iter,
            trap_radius,
            palette_t, palette_idx,
            energy, effective_beat if not in_close else 0.0,
        )
        if watermark_data is not None:
            w_rgb, w_alpha, wy1, wy2, wx1, wx2 = watermark_data
            for c in range(3):
                frame[wy1:wy2, wx1:wx2, c] = (w_alpha * w_rgb[:, :, c] + (1.0 - w_alpha) * frame[wy1:wy2, wx1:wx2, c])
        all_frames.append(frame)

        if (fi + 1) % (fps * 5) == 0 or fi == render_limit - 1:
            pct = 100.0 * (fi + 1) / render_limit
            print(f"  {fi + 1}/{render_limit} frames ({pct:.0f}%)")

    # ── Assemble video ────────────────────────────────────────────────────
    print("\nAssembling video...")
    frame_dur = 1.0 / fps
    clips  = [ImageClip(f, duration=frame_dur) for f in all_frames]
    video  = concatenate_videoclips(clips, method="compose").with_fps(fps)

    audio      = AudioFileClip(audio_path)
    # Cap audio to whichever is shorter: the track or what we actually rendered
    audio_end  = min(audio.duration, render_dur, duration)
    audio      = audio.subclipped(0, audio_end)

    if loop_close_secs > 0 and render_limit > total_frames:
        # Fade the last 2 s of music out before the silent outro
        fade_dur = min(2.0, audio_end)
        audio    = audio.audio_fadeout(fade_dur)

    # Attach audio; video frames beyond audio duration play in silence
    # Set the duration of the audio to match the video clip using CompositeAudioClip to guarantee silence at the end
    padded_audio = CompositeAudioClip([audio]).with_duration(video.duration)
    video = video.with_audio(padded_audio)

    print(f"\nEncoding to {output_path} ...")
    import sys
    if sys.platform == "darwin":
        vcodec = "hevc_videotoolbox"
        preset = None
        ffmpeg_params = ["-q:v", "65"]
    else:
        vcodec = "libx265"
        preset = "medium"
        ffmpeg_params = ["-crf", "18"]

    video.write_videofile(
        output_path,
        fps=fps,
        codec=vcodec,
        audio_codec="aac",
        preset=preset,
        ffmpeg_params=ffmpeg_params,
        threads=os.cpu_count() or 4,
    )

    video.close()
    audio.close()
    print(f"\nDone! Video saved to: {output_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Generate a fractal flame space-nebula music video or single image."
    )

    # --- Shared arguments ---
    parser.add_argument("--output",   "-o", default="",
                        help="Output file path.")
    parser.add_argument("--width",    "-W", type=int, default=0)
    parser.add_argument("--height",   "-H", type=int, default=0)
    parser.add_argument("--randomize", "-r", action="store_true",
                        help="Randomly select fractal preset and palette.")

    # --- Image mode ---
    parser.add_argument("--image", action="store_true",
                        help="Render a single fractal image (PNG) instead of a video.")
    parser.add_argument("--preset", type=int, default=None,
                        help="Julia preset index (0-%d). Use with --image." % (len(JULIA_PRESETS) - 1))
    parser.add_argument("--cx", type=float, default=None,
                        help="Custom Julia c real part.")
    parser.add_argument("--cy", type=float, default=None,
                        help="Custom Julia c imaginary part.")
    parser.add_argument("--palette", type=int, default=None,
                        help="Palette index (0-%d)." % (len(PALETTES) - 1))
    parser.add_argument("--zoom", type=float, default=1.5,
                        help="Zoom level (default: 1.5).")
    parser.add_argument("--rotation", type=float, default=0.0,
                        help="Rotation in radians (default: 0).")
    parser.add_argument("--max-iter", type=int, default=80,
                        help="Max iterations (default: 80).")
    parser.add_argument("--trap-radius", type=float, default=0.85,
                        help="Orbit trap radius (default: 0.85).")
    parser.add_argument("--energy", type=float, default=0.3,
                        help="Simulated audio energy for glow (default: 0.3).")
    parser.add_argument("--brightness", type=float, default=1.0,
                        help="Brightness multiplier for image output. "
                             "1.0 = default, 1.5 = brighter, 0.5 = darker.")

    # --- Video mode ---
    parser.add_argument("--audio",    "-a", default="",
                        help="Path to the audio file (required for video mode).")
    parser.add_argument("--fps",            type=int, default=30)
    parser.add_argument("--sections", "-s", type=int, default=0,
                        help="Number of fractal sections (0 = auto).")
    parser.add_argument("--loop-close", "-l", type=float, default=0.0,
                        metavar="SECS",
                        help="Append SECS seconds of silent outro that "
                             "smoothly transitions the final fractal back "
                             "to the opening shape, closing the visual loop. "
                             "Recommended: 3–8. Default: 0 (disabled).")
    parser.add_argument("--render-secs", "-t", type=float, default=0.0,
                        metavar="SECS",
                        help="Only render the first SECS seconds of video "
                             "(0 = full video). Useful for quick previews.")
    parser.add_argument("--beat-strength", "-b", type=float, default=1.0,
                        metavar="MULT",
                        help="Multiplier on all beat-driven effects: zoom "
                             "retraction, trap-radius breathing, glow "
                             "brightness, and beat flash. "
                             "1.0 = default, 0.0 = no beat response, "
                             "2.0 = intense. (default: 1.0)")
    parser.add_argument("--beat-dynamics", "-d", type=float, default=0.0,
                        metavar="GATE",
                        help="How much the current RMS energy gates the beat "
                             "pulse. 0.0 = every beat hits equally hard; "
                             "1.0 = beat strength fully tracks the RMS level "
                             "so quiet passages pulse gently and loud ones "
                             "pulse hard. Values between give partial gating. "
                             "(default: 0.0)")
    parser.add_argument("--preset-file", "-p", default="",
                        metavar="PATH",
                        help="JSON file to save/load the preset sequence.")
    parser.add_argument("--watermark", default="music_to_sleep_to_profile.png",
                        help="Path to transparent PNG watermark (default: music_to_sleep_to_profile.png).")
    parser.add_argument("--negative-logo", action="store_true",
                        help="Invert the watermark logo colors (negative).")

    args = parser.parse_args()

    if args.image:
        # --- Image mode ---
        output = args.output or "fractal.png"
        w = args.width or 3840
        h = args.height or 2160
        render_fractal_image(
            output_path=output,
            width=w,
            height=h,
            preset_index=args.preset,
            cx=args.cx,
            cy=args.cy,
            randomize=args.randomize,
            palette_index=args.palette,
            zoom=args.zoom,
            rotation=args.rotation,
            max_iter=args.max_iter,
            trap_radius=args.trap_radius,
            energy=args.energy,
            brightness=args.brightness,
        )
    else:
        # --- Video mode ---
        if not args.audio:
            sys.exit("Error: --audio/-a is required for video mode. "
                     "Use --image for single image generation.")
        if not os.path.isfile(args.audio):
            sys.exit(f"Error: audio file not found: {args.audio}")

        output = args.output or "fractal_video.mp4"
        w = args.width or 1920
        h = args.height or 1080

        create_fractal_video(
            audio_path=args.audio,
            output_path=output,
            width=w,
            height=h,
            fps=args.fps,
            num_sections=args.sections,
            loop_close_secs=args.loop_close,
            render_secs=args.render_secs,
            beat_strength=args.beat_strength,
            beat_dynamics=args.beat_dynamics,
            randomize=args.randomize,
            preset_file=args.preset_file,
            watermark=args.watermark,
            negative_logo=args.negative_logo,
        )


if __name__ == "__main__":
    main()
