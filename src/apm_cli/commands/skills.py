"""APM skills command group.

Discovery surface for skills published on skills.sh.  Mirrors the
shape of :mod:`apm_cli.commands.mcp`: a thin Click wrapper that defers
to a registry client and renders results either via Rich (interactive)
or with plain ``click.echo`` (CI / non-TTY).
"""

import os
import sys

import click

from ..core.command_logger import CommandLogger
from ._helpers import _get_console

SKILLS_REGISTRY_ENV = "SKILLS_REGISTRY_URL"


def _build_client_with_diag(console, logger):
    """Construct a ``SimpleSkillsClient`` honouring ``SKILLS_REGISTRY_URL``.

    Mirrors ``apm.commands.mcp._build_registry_with_diag``: defaults stay
    quiet, overrides emit a one-line diagnostic so enterprise users can
    confirm they are hitting the override.
    """
    from ..registry.skills_client import SimpleSkillsClient

    client = SimpleSkillsClient()
    override = os.environ.get(SKILLS_REGISTRY_ENV)
    if override:
        url = client.registry_url
        if console:
            console.print(f"[muted]Skills registry: {url}[/muted]")
        else:
            logger.progress(f"Skills registry: {url}")
    return client


def _handle_registry_network_error(exc, client, console, logger, action):
    """Render a skills-registry network failure with env-var-aware guidance."""
    if client is None:
        return False
    url = client.registry_url
    override = os.environ.get(SKILLS_REGISTRY_ENV)
    if override:
        hint = f"{SKILLS_REGISTRY_ENV} is set -- verify the URL is correct and reachable."
    else:
        hint = "The registry may be temporarily unavailable. Retry shortly."

    msg = f"Could not {action} skills registry at {url}"
    if console:
        from ..utils.console import STATUS_SYMBOLS

        console.print(f"\n{STATUS_SYMBOLS['error']} {msg}", style="red")
        console.print(f"  -> {hint}", style="dim")
    else:
        logger.error(msg)
        logger.error(hint)
    return True


@click.group(help="Discover skills published on skills.sh")
def skills():
    """Manage skill discovery via the skills.sh directory."""
    pass


@skills.command(help="Search skills on skills.sh")
@click.argument("query", required=True)
@click.option("--limit", default=10, show_default=True, help="Number of results to show")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
@click.pass_context
def search(ctx, query, limit, verbose):
    """Search skills.sh for skills matching ``query``."""
    logger = CommandLogger("skills-search", verbose=verbose)
    client = None
    try:
        console = _get_console()
        client = _build_client_with_diag(console, logger)
        results = client.search_skills(query)[:limit]

        if not console:
            logger.progress(f"Searching for: {query}", symbol="search")
            if not results:
                logger.warning("No skills found")
                return
            for skill in results:
                name = skill.get("name") or skill.get("skillId") or "Unknown"
                source = skill.get("source", "unknown source")
                installs = skill.get("installs", 0)
                click.echo(f"  {name}")
                click.echo(f"    source: {source}  installs: {installs}")
            return

        console.print("\n[bold cyan]Skills.sh Search[/bold cyan]")
        console.print(f"[muted]Query: {query}[/muted]")

        if not results:
            console.print(
                f"\n[yellow][!][/yellow] No skills found matching '[bold]{query}[/bold]'"
            )
            console.print("\n[muted] Try broader search terms or check the spelling[/muted]")
            return

        total_shown = len(results)
        console.print(
            f"\n[green]+[/green] Found [bold]{total_shown}[/bold] "
            f"skill{'s' if total_shown != 1 else ''}"
        )

        from rich.table import Table

        table = Table(show_header=True, header_style="bold cyan", border_style="cyan")
        table.add_column("Name", style="bold white", no_wrap=True, min_width=20)
        table.add_column("Source", style="white", ratio=1)
        table.add_column("Installs", style="cyan", justify="right", min_width=8)

        for skill in results:
            name = skill.get("name") or skill.get("skillId") or "Unknown"
            source = skill.get("source", "unknown")
            installs = skill.get("installs", 0)
            try:
                installs_str = f"{int(installs):,}"
            except (TypeError, ValueError):
                installs_str = str(installs)

            if len(source) > 60:
                truncate_pos = 57
                if " " in source[50:65]:
                    space_pos = source.rfind(" ", 50, 65)
                    if space_pos > 50:
                        truncate_pos = space_pos
                source = source[:truncate_pos] + "..."

            table.add_row(name, source, installs_str)

        console.print(table)

        console.print(
            "\n[muted] Visit [bold cyan]https://skills.sh[/bold cyan] for installation details[/muted]"
        )
        if total_shown == limit:
            console.print(
                f"[muted]   Use [bold cyan]--limit {limit * 2}[/bold cyan] "
                "to see more results[/muted]"
            )

    except ValueError as e:
        # Empty query (raised by the client) is a user error, not a server error.
        logger.error(str(e))
        sys.exit(1)
    except Exception as e:
        try:
            import requests

            if isinstance(e, requests.RequestException) and _handle_registry_network_error(
                e, client, _get_console(), logger, "reach"
            ):
                sys.exit(1)
        except ImportError:
            pass
        logger.error(f"Error searching skills.sh: {e}")
        sys.exit(1)
