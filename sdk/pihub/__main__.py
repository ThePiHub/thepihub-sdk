"""`python -m pihub ...`, for a machine where the `pihub` script is not on PATH."""

import sys

from pihub.cli import main

sys.exit(main())
