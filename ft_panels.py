"""
ft_panels.py — reusable panel state/view abstractions for FileTagger apps.

Phase P1:
- Defines PanelState and PanelView as standalone data structures.
- Provides helpers for main panels, floating panels and zoom-style panels.
- Does not yet replace FT.py's existing flags. This is a safe foundation layer.

Long-term intent:
    PanelState  = what is being shown
    PanelView   = where/how it is shown
    Renderer    = how thumbnails/images are drawn
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple


SOURCE_FOLDER = "folder"
SOURCE_COLLECTION = "collection"
SOURCE_CULL = "cull"
SOURCE_SIMILAR = "similar"
SOURCE_GROUP_SUMMARY = "group_summary"
SOURCE_GROUP = "group"
SOURCE_LOCATED = "located"
SOURCE_ZOOM = "zoom"
SOURCE_FLOATING = "floating"


@dataclass
class PanelState:
    """Data and selection state for one visible panel.

    This intentionally contains no Tk widgets. It can therefore describe:
    - the main thumbs area
    - either side of a split view
    - a floating comparison panel
    - a zoom-like single-image panel
    """

    source_type: str = SOURCE_FOLDER
    files: List[str] = field(default_factory=list)
    selected: Set[str] = field(default_factory=set)
    placed: List[str] = field(default_factory=list)
    cell_size: int = 250
    header_text: str = ""
    source_path: str = ""
    decorations: Dict[str, dict] = field(default_factory=dict)
    history: List["PanelState"] = field(default_factory=list)

    def copy_shallow(self) -> "PanelState":
        """Return a shallow copy suitable for history/back navigation."""
        return PanelState(
            source_type=self.source_type,
            files=list(self.files),
            selected=set(self.selected),
            placed=list(self.placed),
            cell_size=self.cell_size,
            header_text=self.header_text,
            source_path=self.source_path,
            decorations=dict(self.decorations),
            history=list(self.history),
        )

    def set_source(self, source_type: str, files=None, *, header_text: str = "",
                   source_path: str = "", cell_size: Optional[int] = None) -> None:
        """Replace this panel's source while preserving the panel object."""
        self.source_type = source_type
        self.files = list(files or [])
        self.selected.clear()
        self.placed.clear()
        self.header_text = header_text
        self.source_path = source_path
        self.decorations.clear()
        if cell_size is not None:
            self.cell_size = cell_size

    def push_history(self) -> None:
        """Save current state for later restoration."""
        self.history.append(self.copy_shallow())

    def pop_history(self) -> bool:
        """Restore previous state. Returns True if restored."""
        if not self.history:
            return False
        prev = self.history.pop()
        self.source_type = prev.source_type
        self.files = prev.files
        self.selected = prev.selected
        self.placed = prev.placed
        self.cell_size = prev.cell_size
        self.header_text = prev.header_text
        self.source_path = prev.source_path
        self.decorations = prev.decorations
        return True


@dataclass
class PanelView:
    """Layout/view description for where a PanelState is displayed."""

    container: str = "main"          # "main", "split_left", "split_right", "floating", "zoom"
    floating: bool = False
    width: Optional[int] = None
    height: Optional[int] = None
    scroll_x: int = 0
    scroll_y: int = 0
    show_nav: bool = True
    show_header: bool = True
    independent_scroll: bool = False


@dataclass
class PanelSlot:
    """Binds panel state to its view description."""

    state: PanelState = field(default_factory=PanelState)
    view: PanelView = field(default_factory=PanelView)


def make_main_panel(files=None, *, header_text: str = "", source_path: str = "",
                    source_type: str = SOURCE_FOLDER, cell_size: int = 250) -> PanelSlot:
    slot = PanelSlot(
        state=PanelState(
            source_type=source_type,
            files=list(files or []),
            cell_size=cell_size,
            header_text=header_text,
            source_path=source_path,
        ),
        view=PanelView(container="main", floating=False),
    )
    return slot


def make_split_panel(side: str, files=None, *, header_text: str = "",
                     source_path: str = "", source_type: str = SOURCE_FOLDER,
                     cell_size: int = 180) -> PanelSlot:
    container = "split_right" if side == "right" else "split_left"
    return PanelSlot(
        state=PanelState(
            source_type=source_type,
            files=list(files or []),
            cell_size=cell_size,
            header_text=header_text,
            source_path=source_path,
        ),
        view=PanelView(container=container, floating=False, independent_scroll=True),
    )


def make_floating_panel(files=None, *, header_text: str = "", source_path: str = "",
                        source_type: str = SOURCE_FLOATING, cell_size: int = 300,
                        width: Optional[int] = None, height: Optional[int] = None) -> PanelSlot:
    return PanelSlot(
        state=PanelState(
            source_type=source_type,
            files=list(files or []),
            cell_size=cell_size,
            header_text=header_text,
            source_path=source_path,
        ),
        view=PanelView(container="floating", floating=True, width=width, height=height,
                       independent_scroll=True),
    )


def make_zoom_panel(path: str, *, cell_size: int = 700,
                    header_text: Optional[str] = None) -> PanelSlot:
    return PanelSlot(
        state=PanelState(
            source_type=SOURCE_ZOOM,
            files=[path] if path else [],
            cell_size=cell_size,
            header_text=header_text if header_text is not None else path,
            source_path=path,
        ),
        view=PanelView(container="zoom", floating=True, show_nav=True,
                       independent_scroll=False),
    )
