"""Entry point for the packaged executable."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from grebels_telemetry.app import main

if __name__ == "__main__":
    main()
