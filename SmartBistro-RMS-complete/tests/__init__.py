"""Test package for SmartBistro RMS

This package contains all unit, integration, and functional tests
for the SmartBistro Restaurant Management System.
"""

import os
import sys
from pathlib import Path

# Add the parent directory to the path to allow imports from the main package
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Test directory paths
TEST_DIR = Path(__file__).parent
FIXTURES_DIR = TEST_DIR / "fixtures"

# Export commonly used test utilities and paths
__all__ = [
    "PROJECT_ROOT",
    "TEST_DIR",
    "FIXTURES_DIR",
]
