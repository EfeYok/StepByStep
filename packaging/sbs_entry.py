"""PyInstaller giriş noktası: sbs.exe bu betikten üretilir."""

import sys

from sbs.cli import main

if __name__ == "__main__":
    sys.exit(main())
