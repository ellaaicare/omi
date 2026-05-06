"""Ella-specific API endpoint package.

Routers are intentionally imported by their concrete module paths in
``ella.__init__``. Avoid importing them here: package-level side effects make
one optional router dependency block unrelated endpoints such as chat.
"""

__all__: list[str] = []
