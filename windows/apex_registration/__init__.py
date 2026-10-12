"""Controller-owned EA registration runtime; Forge is accessed only over HTTP."""

def check_runtime():
    """Import validation only: no browser, network or account operation."""
    from . import cli
    from .flows.ea import ea_pw_full_setup
    from .flows.steam import try_steam_once
