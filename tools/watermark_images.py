#!/usr/bin/env python3
"""
Watermark Images Utility
Applies a watermark to one or more images matching the size and bottom-right
placement used in the music video pipeline (12% height, 2% padding, 40% opacity).

Usage:
    python watermark_images.py --input /path/to/folder --output /path/to/folder/watermarked
    python watermark_images.py --input /path/to/image.jpg --output /path/to/output.jpg
"""

import os
import sys
import argparse
import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DEFAULT_WATERMARK = os.path.join(ROOT, "assets", "music_to_sleep_to_profile.png")
if not os.path.exists(DEFAULT_WATERMARK):
    DEFAULT_WATERMARK = os.path.join(ROOT, "music_to_sleep_to_profile.png")

def load_watermark_data(watermark_path: str, img_w: int, img_h: int, negative: bool = False) -> tuple | None:
    if not watermark_path or watermark_path.lower() in ("none", "") or not os.path.exists(watermark_path):
        return None
    try:
        logo = cv2.imread(watermark_path, cv2.IMREAD_UNCHANGED)
        if logo is not None and len(logo.shape) == 3 and logo.shape[2] == 4:
            # Resize logo to fit in bottom right corner (12% of image height)
            w_h = int(img_h * 0.12)
            aspect_ratio = logo.shape[1] / logo.shape[0]
            w_w = int(w_h * aspect_ratio)
            logo = cv2.resize(logo, (w_w, w_h))
            
            logo_bgr = logo[:, :, :3]
            if negative:
                logo_bgr = 255 - logo_bgr
                
            logo_alpha = (logo[:, :, 3] / 255.0) * 0.4  # 40% opacity
            
            # Bottom-right position with 2% padding
            pad_x = int(img_w * 0.02)
            pad_y = int(img_h * 0.02)
            y1 = img_h - pad_y - w_h
            y2 = img_h - pad_y
            x1 = img_w - pad_x - w_w
            x2 = img_w - pad_x
            
            return (logo_bgr, logo_alpha, y1, y2, x1, x2)
    except Exception as e:
        print(f"Warning: could not load watermark: {e}")
    return None

def apply_watermark(img: np.ndarray, watermark_data: tuple | None) -> np.ndarray:
    if watermark_data is not None:
        w_bgr, w_alpha, wy1, wy2, wx1, wx2 = watermark_data
        for c in range(3):
            img[wy1:wy2, wx1:wx2, c] = (w_alpha * w_bgr[:, :, c] + (1.0 - w_alpha) * img[wy1:wy2, wx1:wx2, c])
    return img

def process_file(src_path: str, dst_path: str, watermark_path: str, negative: bool = False):
    img = cv2.imread(src_path)
    if img is None:
        print(f"  Error: cannot read image: {src_path}")
        return False
        
    h, w = img.shape[:2]
    watermark_data = load_watermark_data(watermark_path, w, h, negative=negative)
    if watermark_data is None:
        print(f"  Error: failed to load watermark metadata for {src_path}")
        return False
        
    img = apply_watermark(img, watermark_data)
    
    # Ensure parent directory of dst_path exists
    os.makedirs(os.path.dirname(os.path.abspath(dst_path)), exist_ok=True)
    
    # Save the watermarked image
    cv2.imwrite(dst_path, img)
    print(f"  Watermarked: {os.path.basename(src_path)} -> {os.path.basename(dst_path)}")
    return True

def main():
    parser = argparse.ArgumentParser(
        description="Apply a bottom-right watermark to images with identical size and padding as the video pipeline."
    )
    parser.add_argument(
        "--input", "-i", required=True,
        help="Input image file path or directory containing images."
    )
    parser.add_argument(
        "--output", "-o", default="",
        help="Output image file path or directory. If omitted, saves to a 'watermarked/' subdirectory in the input folder."
    )
    parser.add_argument(
        "--watermark", "-w", default=DEFAULT_WATERMARK,
        help=f"Path to transparent PNG watermark (default: {DEFAULT_WATERMARK})."
    )
    parser.add_argument(
        "--negative-logo", action="store_true",
        help="Invert the watermark colors (negative)."
    )
    
    args = parser.parse_args()
    
    if not os.path.exists(args.input):
        sys.exit(f"Error: input path does not exist: {args.input}")
        
    if not os.path.exists(args.watermark):
        sys.exit(f"Error: watermark file not found: {args.watermark}")
        
    if os.path.isdir(args.input):
        # Folder mode
        src_dir = os.path.abspath(args.input)
        if args.output:
            dst_dir = os.path.abspath(args.output)
        else:
            dst_dir = os.path.join(src_dir, "watermarked")
            
        print(f"Scanning directory: {src_dir}")
        valid_exts = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff")
        files = [
            f for f in os.listdir(src_dir)
            if f.lower().endswith(valid_exts) and os.path.isfile(os.path.join(src_dir, f))
        ]
        
        if not files:
            print("No valid images found in input folder.")
            return
            
        print(f"Found {len(files)} images. Watermarking to: {dst_dir}")
        success_count = 0
        for f in files:
            src_file = os.path.join(src_dir, f)
            dst_file = os.path.join(dst_dir, f)
            if process_file(src_file, dst_file, args.watermark, args.negative_logo):
                success_count += 1
                
        print(f"\nDone! Successfully watermarked {success_count}/{len(files)} images.")
        
    else:
        # Single file mode
        src_file = os.path.abspath(args.input)
        if args.output:
            dst_file = os.path.abspath(args.output)
        else:
            base, ext = os.path.splitext(src_file)
            dst_file = f"{base}_watermarked{ext}"
            
        print(f"Watermarking single image: {src_file}")
        if process_file(src_file, dst_file, args.watermark, args.negative_logo):
            print("Done!")
        else:
            sys.exit("Failed to watermark image.")

if __name__ == "__main__":
    main()
