# Music Video Pipeline Manager

import os
import sys

import shutil

def get_binary_path(binary_name: str) -> str:
    """
    Get the path to a binary. Checks if running inside PyInstaller
    and looks in the bundled bin/ directory, falling back to system path or standard macOS brew paths.
    """
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        bundled_path = os.path.join(sys._MEIPASS, "bin", binary_name)
        if os.path.exists(bundled_path):
            return bundled_path

    found = shutil.which(binary_name)
    if found:
        return found

    for fallback_dir in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"):
        p = os.path.join(fallback_dir, binary_name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p

    return binary_name

