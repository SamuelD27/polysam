"""Pytest configuration: ensure project root is on sys.path."""
import sys
from pathlib import Path

# Add project root so `experiments` package is importable
_root = Path(__file__).resolve().parents[3]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
