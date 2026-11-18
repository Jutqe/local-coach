#!/usr/bin/env python3
"""Local Coach 5 — точка входа. Запуск: python3 coach.py [команда]; список команд: python3 coach.py -h"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from localcoach.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
