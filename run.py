#!/usr/bin/env python3
"""
Music Video Pipeline Manager — Entry Point

Launch the GUI application:
    python run.py
"""

import os
import sys

# Ensure the project root is on the path
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pipeline.app import PipelineApp


def main():
    app = PipelineApp()
    app.mainloop()


if __name__ == "__main__":
    main()
