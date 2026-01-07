# SPDX-License-Identifier: GPL-2.0-only

"""
Shared utilities for labgrid strategies
"""

# Try to import never_retry, but provide a fallback if not available
# This handles different labgrid versions
try:
    from labgrid.strategy import never_retry
except ImportError:
    try:
        from labgrid.strategy.common import never_retry
    except ImportError:
        # Fallback: define a no-op decorator if never_retry is not available
        def never_retry(func):
            return func

__all__ = ['never_retry']
