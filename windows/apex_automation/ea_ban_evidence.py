"""Spatial evidence for the EA restriction dialog, independent of Win32."""
from .ea_pages import ACCOUNT_BANNED_TERMS, phrase_point


def ban_dialog_visible(tokens, rect) -> bool:
    # A ban heading in a terminal, a stale code, or an empty library is not
    # enough. Require the dialog's title, explanation/code and Close control.
    tokens = tuple(t for t in tokens if t.roi is not None and t.confidence >= .85)
    headings = tuple(t for t in ACCOUNT_BANNED_TERMS if "ec107" not in t)
    heading = any(phrase_point(tokens, rect, text,
                              x_range=(.15, .90), y_range=(.15, .70), exact=True)
                  for text in headings)
    detail = any(phrase_point(tokens, rect, text,
                             x_range=(.15, .90), y_range=(.20, .90))
                 for text in ("errorcodeec107", "ec107", "onlineandsocialfeatures",
                              "appealtheban", "youcanstillplayyourpurchasedgames"))
    close = any(phrase_point(tokens, rect, text,
                            x_range=(.35, .90), y_range=(.40, .95), exact=True)
                for text in ("close", "关闭"))
    return bool(heading and detail and close)
