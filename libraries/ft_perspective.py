"""
ft_perspective.py — vertical/horizontal perspective correction for FTImgedit.

Separate from the 4-point Transform tool:
- Transform = document/page rectification.
- Perspective = keystone correction with an axis selector and slider.
"""

from __future__ import annotations

import math
from PIL import Image


def _require_cv2():
    try:
        import cv2
        import numpy as np
        return cv2, np
    except Exception as exc:
        raise RuntimeError("OpenCV is required for Perspective. Install opencv-python.") from exc


def perspective_adjust(image: Image.Image, amount: float, *, axis: str = "vertical",
                       keep_size: bool = True, border=(128, 128, 128)) -> Image.Image:
    """Apply vertical or horizontal keystone correction.

    axis="vertical":
        Positive = top outward, bottom inward.
        Negative = top inward, bottom outward.

    axis="horizontal":
        Positive = left outward, right inward.
        Negative = left inward, right outward.
    """
    cv2, np = _require_cv2()

    if image is None:
        raise ValueError("No image supplied.")

    amount = max(-1.0, min(1.0, float(amount)))
    if abs(amount) < 1e-6:
        return image.copy()

    img = image.convert("RGB")
    w, h = img.size
    axis = (axis or "vertical").lower()

    src = np.float32([[0.0, 0.0], [float(w), 0.0], [float(w), float(h)], [0.0, float(h)]])

    if axis.startswith("h"):
        left_shift = h * 0.33 * amount
        right_shift = h * 0.13 * amount
        dst = np.float32([
            [0.0, -left_shift],
            [float(w), right_shift],
            [float(w), float(h) - right_shift],
            [0.0, float(h) + left_shift],
        ])
    else:
        top_shift = w * 0.33 * amount
        bottom_shift = w * 0.13 * amount
        dst = np.float32([
            [-top_shift, 0.0],
            [float(w) + top_shift, 0.0],
            [float(w) - bottom_shift, float(h)],
            [bottom_shift, float(h)],
        ])

    H = cv2.getPerspectiveTransform(src, dst)

    if keep_size:
        out_w, out_h = w, h
        M = H
    else:
        corners = src.reshape(-1, 1, 2)
        warped_corners = cv2.perspectiveTransform(corners, H).reshape(-1, 2)
        min_x = float(warped_corners[:, 0].min()) - 2.0
        min_y = float(warped_corners[:, 1].min()) - 2.0
        max_x = float(warped_corners[:, 0].max()) + 2.0
        max_y = float(warped_corners[:, 1].max()) + 2.0

        out_w = max(2, int(math.ceil(max_x - min_x)))
        out_h = max(2, int(math.ceil(max_y - min_y)))

        T = np.array([[1.0, 0.0, -min_x], [0.0, 1.0, -min_y], [0.0, 0.0, 1.0]], dtype=np.float64)
        M = T @ H

    arr = np.array(img)
    warped = cv2.warpPerspective(
        arr,
        M,
        (out_w, out_h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=tuple(int(x) for x in border),
    )

    return Image.fromarray(warped)


def vertical_perspective(image: Image.Image, amount: float, *, keep_size: bool = True,
                         border=(128, 128, 128)) -> Image.Image:
    """Backward-compatible wrapper."""
    return perspective_adjust(image, amount, axis="vertical", keep_size=keep_size, border=border)
