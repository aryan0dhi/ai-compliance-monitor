"""Entry point for the background worker: `python -m app.worker`."""

import sys

from app.modules.worker.runner import main

if __name__ == "__main__":
    sys.exit(main())
