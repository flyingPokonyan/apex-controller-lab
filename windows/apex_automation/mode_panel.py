"""Locate the bot-royale card by the words on it, not by a measured point.

The mode panel has been moved twice. A fixed crop of the title now reads only
the tail 「大逃杀」 and the old click lands on the wrong card. The search is
deliberately a region of the panel, not the whole frame: the lobby underneath
also prints mode names, and a full-frame search would click those.
"""

from __future__ import annotations

from .ocr_obstacles import OcrToken, normalize_ocr_text


# Shortest substring that distinguishes this card from every other card on the
# panel. The painted title is 「进化版机器人大逃杀」, so the token is not equal
# to these three characters and must not be matched exactly.
BOT_TITLE_WORD = "机器人"
# Appears under the hovered card. Other cards print the same words, so a hit
# only counts when it sits on the same card as the title.
CONFIRM_WORD = "选择模式"
# A training-column card is a few hundred pixels across. The featured card in
# the middle column is much farther away than this.
NEAR_DX = 420
NEAR_DY = 240


def token_center(token: OcrToken) -> tuple[int, int]:
    assert token.roi is not None
    x1, y1, x2, y2 = token.roi
    return (x1 + x2) // 2, (y1 + y2) // 2


def _contains(token: OcrToken, word: str) -> bool:
    folded = normalize_ocr_text(word)
    return bool(folded) and folded in token.normalized


def on_same_card(title: OcrToken, other: OcrToken, *, near_dx: int = NEAR_DX, near_dy: int = NEAR_DY) -> bool:
    """True when `other` is the confirm line of the card `title` belongs to."""

    if title.roi is None or other.roi is None:
        return False
    tx, ty = token_center(title)
    ox, oy = token_center(other)
    return abs(ox - tx) <= near_dx and -40 <= (oy - ty) <= near_dy


def matching_text_token(
    tokens: tuple[OcrToken, ...],
    words: tuple[str, ...],
    *,
    exact: bool = False,
    near_anchors: tuple[str, ...] = (),
    near_dx: int = NEAR_DX,
    near_dy: int = NEAR_DY,
) -> tuple[OcrToken | None, bool]:
    """Pick the token to click, or report that a hit had no coordinates.

    Words are tried in the order the profile lists them. A token matches when
    it contains the word; `exact` additionally requires the whole token to be
    that word. 「机器人」 must not use exact matching: the title token is the
    longer 「进化版机器人大逃杀」, and a split read is still valid as long as
    one piece contains the word. The returned flag is true when a textual hit
    was dropped only because the engine gave no box.
    """

    anchors = [
        token
        for token in tokens
        if token.roi is not None and any(_contains(token, word) for word in near_anchors)
    ]
    missing_position = False
    for word in words:
        folded = normalize_ocr_text(word)
        if not folded:
            continue
        for token in tokens:
            if folded not in token.normalized:
                continue
            if exact and folded != token.normalized:
                continue
            if token.roi is None:
                missing_position = True
                continue
            if near_anchors and not any(
                on_same_card(anchor, token, near_dx=near_dx, near_dy=near_dy)
                for anchor in anchors
            ):
                continue
            return token, missing_position
    return None, missing_position
