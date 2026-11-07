"""Bounded, read-only recovery for transient Maa screenshot geometry changes.

Every attempt obtains a fresh frame. A frame is never resized, padded, reused,
or returned unless the native window geometry is valid on both sides of its
capture. Persistent geometry changes still stop the caller safely.
"""
from __future__ import annotations

import time


MAX_CAPTURE_ATTEMPTS = 3
CAPTURE_RETRY_DELAY_SECONDS = 0.15
EXPECTED_CAPTURE_SIZE = (1040, 1920)


def capture_with_retry(get_frame, geometry_ok, *, sleep=time.sleep):
    """Return a fresh, full-sized nonblack frame, or raise after three attempts.

    ``get_frame`` and ``geometry_ok`` must be read-only callbacks. Unexpected
    callback exceptions propagate immediately; only failed frame/geometry
    checks are retried. No screenshot or input APIs are imported here.
    """
    failure = "MAA_CAPTURE_UNAVAILABLE"
    for attempt in range(MAX_CAPTURE_ATTEMPTS):
        geometry_before = geometry_ok()
        image = get_frame()
        geometry_after = geometry_ok()
        shape = None if image is None else image.shape
        if not geometry_before or not geometry_after:
            failure = "LAYOUT_CHANGED_STOP"
        elif (shape is None or len(shape) != 3
              or shape[:2] != EXPECTED_CAPTURE_SIZE or shape[2] not in (3, 4)):
            failure = f"MAA_CAPTURE_DIMENSIONS:{shape}"
        elif image[:, :, :3].max() == 0:
            failure = "MAA_BLACK_FRAME"
        else:
            return image
        if attempt + 1 < MAX_CAPTURE_ATTEMPTS:
            sleep(CAPTURE_RETRY_DELAY_SECONDS)
    raise RuntimeError(failure)
