"""Module entrypoint for `python -m hs_coach`."""

import sys

from hs_coach.cli import main

if __name__ == "__main__":
    sys.exit(main())
