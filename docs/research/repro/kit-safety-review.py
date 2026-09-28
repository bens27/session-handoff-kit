#!/usr/bin/env python3
"""Run the retained safety audit through its CI regression suite."""
from pathlib import Path
import runpy
runpy.run_path(str(Path(__file__).resolve().parents[3] / 'tests/test_safety.py'), run_name='__main__')
