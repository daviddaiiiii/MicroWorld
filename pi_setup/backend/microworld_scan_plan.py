"""Pure scan-plan helpers shared by the Pi scan wrapper and offline tests.

The microscope server is deliberately kept out of this module.  A scan plan
is just geometry: the Pi still owns acquisition, while the Mac owns the final
registration and fusion.
"""

from __future__ import annotations

import math
from typing import Dict, List, Tuple


GridKey = Tuple[int, int]
REFERENCE_FOV = (640, 480)
DEFAULT_OVERLAP = 0.25
FIXED_STRIDE = (2000, 1500, 100)


def stride_for_overlap(px_per_step: Tuple[float, float], overlap: float) -> Tuple[int, int]:
    """Convert calibrated distances in reference pixels to motor steps."""
    if not math.isfinite(overlap) or not 0.10 <= overlap <= 0.55:
        raise ValueError("Scan overlap must be between 10% and 55%.")
    if any(not math.isfinite(scale) or scale <= 0 for scale in px_per_step):
        raise ValueError("Scan calibration requires a positive scale on both axes.")
    return tuple(max(1, round(span * (1.0 - overlap) / scale))
                 for span, scale in zip(REFERENCE_FOV, px_per_step))


def overlap_for_stride(stride_xy, px_per_step):
    """Report the overlap implied by an existing scan's measured geometry."""
    return tuple(1.0 - abs(step * scale) / span
                 for step, scale, span in zip(stride_xy, px_per_step, REFERENCE_FOV))


def spiral_grid_order(nx: int, ny: int) -> List[GridKey]:
    """Return every grid cell once, from the centre out in clockwise rings.

    For an even-sized grid the four innermost cells are the first ring.  The
    order is deterministic and keeps each ring spatially coherent, which is
    useful both for focus seeding and for keeping the stage path short.
    """

    nx = max(1, int(nx))
    ny = max(1, int(ny))
    cx = (nx - 1) / 2.0
    cy = (ny - 1) / 2.0
    cells = [(ix, iy) for iy in range(ny) for ix in range(nx)]
    rings: Dict[float, List[GridKey]] = {}
    for ix, iy in cells:
        # Euclidean shells keep the true centre pair first for rectangular or
        # even grids (a Chebyshev shell would put a distant corner beside it).
        ring = round(math.hypot(ix - cx, iy - cy), 6)
        rings.setdefault(ring, []).append((ix, iy))

    order: List[GridKey] = []
    for ring in sorted(rings):
        group = rings[ring]
        if ring == 0:
            group.sort()
        else:
            # Start at the north-west corner of each ring, then walk
            # clockwise.  atan2's range is [-pi, pi]; this key rotates the
            # usual angle so -135 degrees is the first point.
            def angle(cell: GridKey) -> float:
                ix, iy = cell
                a = math.atan2(iy - cy, ix - cx)
                return (a + 3.0 * math.pi / 4.0) % (2.0 * math.pi)

            group.sort(key=lambda cell: (angle(cell), cell[1], cell[0]))
        order.extend(group)
    return order


def center_hub_spec(
    nx: int, ny: int, origin_xy: Tuple[int, int], stride_xy: Tuple[int, int]
) -> Dict[str, object]:
    """Describe the physical centre tile used as a registration hub.

    Odd-by-odd grids already contain an integer centre cell, so an additional
    exposure would be redundant.  Even grids get one extra tile at the exact
    half-step centre between the four inner cells.
    """

    nx = max(1, int(nx))
    ny = max(1, int(ny))
    ox, oy = int(origin_xy[0]), int(origin_xy[1])
    sx, sy = int(stride_xy[0]), int(stride_xy[1])
    gx = (nx - 1) / 2.0
    gy = (ny - 1) / 2.0
    extra = not (nx % 2 == 1 and ny % 2 == 1)
    return {
        "extra": extra,
        "grid_position": [gx, gy],
        "stage_xy": [int(round(ox + gx * sx)), int(round(oy + gy * sy))],
        "nx": nx,
        "ny": ny,
    }
