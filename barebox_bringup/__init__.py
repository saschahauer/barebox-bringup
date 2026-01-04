# SPDX-License-Identifier: GPL-2.0-only

"""
Barebox Hardware Bringup Tool

Brings up barebox on target hardware using labgrid and provides raw console access.
"""

__version__ = "0.1.0"

from .cli import main

__all__ = ["main"]
