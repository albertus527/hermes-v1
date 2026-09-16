"""Canonical Website Builder executable entrypoint.

Usage:
    cd website-builder
    python -m app
"""

from app.runtime import main

sys_exit = main()
raise SystemExit(sys_exit)
