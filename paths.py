"""
Where Arctis Control keeps things, the same whether it runs from source or as the
PyInstaller-built ArctisControl.exe:

  DATA_DIR  %APPDATA%\\ArctisControl   settings, battery history, mixer state, log
  RES_DIR   bundled read-only files (icon); next to the .py files when run from source
"""
import os
import sys
from pathlib import Path

DATA_DIR = Path(os.environ.get("APPDATA", Path.home())) / "ArctisControl"
DATA_DIR.mkdir(parents=True, exist_ok=True)

RES_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
