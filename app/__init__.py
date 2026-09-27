"""Plumb: a hackathon submission and judging portal whose results you can check."""

import os

# The normalization model solves many small (tens to hundreds of rows) linear
# systems. Multithreaded BLAS spends far longer waking threads than doing the
# arithmetic: pinning it to one thread made a fit on the fixture data 60x
# faster (365 ms to 6 ms). This must run before numpy is first imported.
for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_var, "1")

__version__ = "0.1.0"
