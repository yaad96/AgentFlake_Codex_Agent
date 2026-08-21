"""Agentic pipeline package.

Present so the modules are importable as ``agentic.*`` (which the tests under
``agentic/tests`` rely on) and so ``python3 -m unittest discover`` / pytest can
collect them from the AF_Codex_Agent root. The modules are still runnable as
plain scripts: each one puts its own directory on ``sys.path`` before importing
its siblings, so ``python3 agentic/agentic_codex_cli.py`` keeps working.
"""
