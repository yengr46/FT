
from __future__ import annotations
import math
from PIL import Image

def _cv():
    import cv2, numpy as np
    return cv2, np

def straight_keystone_correct(image, quad):
    cv2, np = _cv()

    img = image.convert("RGB")
    w, h = img.size

    tl, tr, br, bl = quad

    top_w = max(1.0, abs(tr[0] - tl[0]))
    bot_w = max(1.0, abs(br[0] - bl[0]))

    ratio = bot_w / top_w

    # conservative correction
    correction = (ratio - 1.0) * 0.55

    mid_y = h * 0.5

    top_expand = correction * w * 0.30
    bot_inset = correction * w * 0.12

    src = np.float32([
        [0, 0],
        [w, 0],
        [w, h],
        [0, h],
    ])

    # midpoint anchored
    dst = np.float32([
        [-top_expand, 0],
        [w + top_expand, 0],
        [w - bot_inset, h],
        [bot_inset, h],
    ])

    H = cv2.getPerspectiveTransform(src, dst)

    corners = np.float32([
        [0,0],[w,0],[w,h],[0,h]
    ]).reshape(-1,1,2)

    warped = cv2.perspectiveTransform(corners, H).reshape(-1,2)

    min_x = warped[:,0].min()
    max_x = warped[:,0].max()
    min_y = warped[:,1].min()
    max_y = warped[:,1].max()

    out_w = int(math.ceil(max_x - min_x))
    out_h = int(math.ceil(max_y - min_y))

    T = np.array([
        [1,0,-min_x],
        [0,1,-min_y],
        [0,0,1]
    ], dtype=float)

    M = T @ H

    arr = np.array(img)

    result = cv2.warpPerspective(
        arr,
        M,
        (out_w, out_h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(128,128,128),
    )

    return Image.fromarray(result)
