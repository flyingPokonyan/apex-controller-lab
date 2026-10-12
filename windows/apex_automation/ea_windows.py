"""Roles of EA top-level windows, independent of geometry and OCR.

Window size is not identity: Qt message receivers can have desktop-sized
default rectangles. Only a named user surface can be restored or selected.
Owned menus/dialogs remain attached to the selected surface, not extra roots.
"""
from __future__ import annotations

from enum import Enum


class EaWindowRole(str, Enum):
    SURFACE = "surface"
    AUXILIARY = "auxiliary"
    UNKNOWN = "unknown"


def window_role(title: str, class_name: str, ex_style: int) -> EaWindowRole:
    title = " ".join(title.casefold().split())
    class_name = class_name.casefold()
    if ("trayiconmessagewindow" in class_name
            or title == "qtrayiconmessagewindow"):
        return EaWindowRole.AUXILIARY
    if ex_style & 0x08000000:  # NOACTIVATE: not an interactive input surface
        return EaWindowRole.UNKNOWN
    # Login dialogs can be TOOLWINDOWs too; that style alone is not identity.
    if title in {"ea", "ea app", "ea desktop", "electronic arts"}:
        return EaWindowRole.SURFACE
    return EaWindowRole.UNKNOWN
