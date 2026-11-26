"""Tests import the three container packages and the shared package directly from the source tree."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
for folder in ('common', 'auth', 'backend', 'worker'):
    if str(ROOT / folder) not in sys.path:
        sys.path.insert(0, str(ROOT / folder))
