"""
ft_thumb_layout.py — shared thumbnail grid sizing for FileTagger apps.

This module owns common thumbnail cell geometry used by FTView/FTnew
and later FT/FTCompare.

Rules:
- no partial columns
- partial bottom row is allowed
- fixed gap between cells
- boundary gap on left/right/top
- cell width/height ratio defaults to 0.85
- image area is square with a fixed margin from cell left/top/right
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil


@dataclass
class ThumbLayout:
    panel_width: int
    panel_height: int
    item_count: int
    columns: int
    rows: int
    gap: int
    boundary_gap: int
    cell_ratio_w_to_h: float
    image_margin: int
    cell_w_px: int
    cell_h_px: int
    image_x_px: int
    image_y_px: int
    image_w_px: int
    image_h_px: int
    outer_rect_w_px: int
    outer_rect_h_px: int
    inner_rect_w_px: int
    inner_rect_h_px: int
    total_w: int
    total_h: int

    @property
    def cell_w(self):
        return self.cell_w_px

    @property
    def cell_h(self):
        return self.cell_h_px

    @property
    def image_w(self):
        return self.image_w_px

    @property
    def image_h(self):
        return self.image_h_px


def calculate_thumb_layout(
    panel_width: int,
    panel_height: int,
    item_count: int,
    columns: int,
    gap: int = 6,
    boundary_gap: int = 3,
    cell_ratio_w_to_h: float = 0.85,
    image_margin: int = 5,
) -> ThumbLayout:
    panel_width = max(1, int(panel_width))
    panel_height = max(1, int(panel_height))
    item_count = max(0, int(item_count))
    columns = max(1, int(columns))
    gap = max(0, int(gap))
    boundary_gap = max(0, int(boundary_gap))
    image_margin = max(0, int(image_margin))
    ratio = float(cell_ratio_w_to_h) if cell_ratio_w_to_h else 0.85
    if ratio <= 0:
        ratio = 0.85

    usable_w = panel_width - (2 * boundary_gap) - ((columns - 1) * gap)
    cell_w = max(20, usable_w // columns)
    cell_h = max(20, int(round(cell_w / ratio)))

    img_w = max(1, cell_w - (2 * image_margin))
    img_h = img_w

    rows = max(1, ceil(item_count / columns)) if item_count else 1
    total_w = (2 * boundary_gap) + (columns * cell_w) + ((columns - 1) * gap)
    total_h = (2 * boundary_gap) + (rows * cell_h) + ((rows - 1) * gap)

    return ThumbLayout(
        panel_width=panel_width,
        panel_height=panel_height,
        item_count=item_count,
        columns=columns,
        rows=rows,
        gap=gap,
        boundary_gap=boundary_gap,
        cell_ratio_w_to_h=ratio,
        image_margin=image_margin,
        cell_w_px=cell_w,
        cell_h_px=cell_h,
        image_x_px=image_margin,
        image_y_px=image_margin,
        image_w_px=img_w,
        image_h_px=img_h,
        outer_rect_w_px=cell_w,
        outer_rect_h_px=cell_h,
        inner_rect_w_px=img_w,
        inner_rect_h_px=img_h,
        total_w=total_w,
        total_h=total_h,
    )


def print_thumb_layout(layout: ThumbLayout, *, prefix: str = "FTView layout") -> None:
    """Console diagnostic used by FTView while tuning thumbnail sizing."""
    try:
        print(
            f"{prefix}: "
            f"columns={layout.columns}, rows={layout.rows}, files={layout.item_count}, "
            f"thumb_area_w={layout.panel_width}, gap={layout.gap}, "
            f"cell={layout.cell_w_px}x{layout.cell_h_px}, "
            f"outer_rect={layout.outer_rect_w_px}x{layout.outer_rect_h_px}, "
            f"inner_rect={layout.inner_rect_w_px}x{layout.inner_rect_h_px}",
            flush=True,
        )
    except Exception:
        pass
