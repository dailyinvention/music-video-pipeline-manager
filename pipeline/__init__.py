# Music Video Pipeline Manager

import os
import sys

def get_binary_path(binary_name: str) -> str:
    """
    Get the path to a binary. Checks if running inside PyInstaller
    and looks in the bundled bin/ directory, falling back to system path.
    """
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        bundled_path = os.path.join(sys._MEIPASS, "bin", binary_name)
        if os.path.exists(bundled_path):
            return bundled_path
    return binary_name
