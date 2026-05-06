"""Strategy layer: ABC + shim re-exports of the canonical classes in ``active_bots``.

Class bodies still live in ``active_bots/*.py`` during the modular-architecture
refactor. The shims import + re-export so callers can write
``from polyhustle.strategies.refined import RefinedStrategy`` today and get
the same class regardless of where its implementation eventually lives.
"""
