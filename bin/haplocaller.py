#!/usr/bin/env python3
"""Launch the guided Haplocaller workflow on a Slurm cluster."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from haplocaller.cli import main

if __name__ == '__main__':
    main()
