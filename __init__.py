"""stackexchange plugin — read-only Stack Exchange API tools.

Tools
-----
``se_search``    search questions (or excerpts) by text/tags on any SE site
``se_question``  one question + body + top answers + comments
``se_similar``   questions similar to a title (error-message shortcut)
``se_sites``     network sites and their ``site`` parameter values

Configuration (all optional)
----------------------------
``STACKEXCHANGE_API_KEY``
    App key in ``~/.hermes/.env`` or the process environment. Raises the daily
    quota from 300 to 10,000 requests.
``stackexchange.api_key`` / ``stackexchange.api_key_file`` in config.yaml
    Alternative places for the key.
``<hermes home>/creds/stackexchange-api-key-lds``
    Default key file location.
"""

import logging
from pathlib import Path

try:
    from . import schemas, tools
except ImportError:  # imported as an orphan module (standalone test runner)
    schemas = None  # type: ignore[assignment]
    tools = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_REGISTRATIONS = (
    ("se_search", "SE_SEARCH"),
    ("se_question", "SE_QUESTION"),
    ("se_similar", "SE_SIMILAR"),
    ("se_sites", "SE_SITES"),
)


def register(ctx):
    """Wire the four read-only tools plus the bundled skill."""
    if not schemas or not tools:
        logger.warning("stackexchange: imported outside a package; skipping registration")
        return

    for tool_name, schema_attr in _REGISTRATIONS:
        handler = getattr(tools, tool_name, None)
        if handler is None:
            logger.error("stackexchange: handler %s missing; tool not registered", tool_name)
            continue
        ctx.register_tool(
            name=tool_name,
            toolset="stackexchange",
            schema=getattr(schemas, schema_attr),
            handler=handler,
            description=getattr(schemas, schema_attr).get("description", ""),
            emoji="📚",
        )

    skills_dir = Path(__file__).parent / "skills"
    if skills_dir.is_dir():
        for child in sorted(skills_dir.iterdir()):
            skill_file = child / "SKILL.md"
            if child.is_dir() and skill_file.exists():
                ctx.register_skill(child.name, skill_file)
                logger.info("stackexchange: registered skill %s", child.name)

    logger.info("stackexchange: registered %d read-only tools", len(_REGISTRATIONS))
