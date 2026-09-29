"""Bundled skills: gmail, google_calendar, github, spotify.

Each skill is a directory with ``SKILL.md`` (frontmatter + instructions)
and ``tools.py`` exposing ``get_tools(config) -> list[Tool]``. The
``agentkai.skills`` loader discovers them here and in ``~/.agentkai/skills/``.

Modules starting with ``_`` (``_http``, ``_common``) are shared helpers,
not skills, and are skipped by discovery.
"""
