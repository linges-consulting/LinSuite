"""Shared shapes for billing's admin CRUD screens — `packages.py`, and any later one cut from
the same cloth. The helpers themselves live in `core/forms.py`; re-exported here so billing's
modules keep one import site.
"""

from core.forms import blank_to_none, refuse, refuse_emptied_field

__all__ = ["blank_to_none", "refuse", "refuse_emptied_field"]
