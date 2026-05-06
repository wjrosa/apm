"""Unit tests for the skills command group (commands/skills.py).

Mirrors the structure of ``test_mcp_command.py``: covers the search
subcommand on Rich + fallback paths, error handling, env-var diagnostics,
and the help surface.
"""

import re
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

from click.testing import CliRunner

from apm_cli.commands.skills import skills

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"https?://[^\s\[\]<>'\"]+")


def _printed_urls(printed: str) -> list:
    """Parse all http(s) URLs out of console-printed text."""
    out = []
    for match in _URL_RE.findall(printed):
        parsed = urlparse(match)
        out.append((parsed.scheme, parsed.hostname))
    return out


FAKE_SKILLS = [
    {
        "id": "github/awesome/multi-stage-dockerfile",
        "skillId": "multi-stage-dockerfile",
        "name": "multi-stage-dockerfile",
        "installs": 12517,
        "source": "github/awesome",
    },
    {
        "id": "sickn33/skills/docker-expert",
        "skillId": "docker-expert",
        "name": "docker-expert",
        "installs": 14354,
        "source": "sickn33/skills",
    },
]


def make_runner():
    return CliRunner()


def patch_client(search_result=None, search_raises=None):
    """Patch ``SimpleSkillsClient`` at its lazy-import location."""
    mock_client = MagicMock()
    if search_raises is not None:
        mock_client.search_skills.side_effect = search_raises
    elif search_result is not None:
        mock_client.search_skills.return_value = search_result
    # Default registry_url so any diag/error rendering has something to print.
    mock_client.registry_url = "https://skills.sh"
    return patch(
        "apm_cli.registry.skills_client.SimpleSkillsClient",
        return_value=mock_client,
    )


# ---------------------------------------------------------------------------
# skills search command
# ---------------------------------------------------------------------------


class TestSkillsSearch:
    def test_search_rich_with_results(self):
        """search with Rich console returns results table."""
        runner = make_runner()
        mock_console = MagicMock()

        with (
            patch_client(search_result=FAKE_SKILLS),
            patch("apm_cli.commands.skills._get_console", return_value=mock_console),
            patch("rich.table.Table", MagicMock()),
        ):
            result = runner.invoke(skills, ["search", "docker"])

        assert result.exit_code == 0
        mock_console.print.assert_called()

    def test_search_rich_no_results(self):
        """search with no results shows a warning via Rich."""
        runner = make_runner()
        mock_console = MagicMock()

        with (
            patch_client(search_result=[]),
            patch("apm_cli.commands.skills._get_console", return_value=mock_console),
        ):
            result = runner.invoke(skills, ["search", "nothing"])

        assert result.exit_code == 0
        printed = " ".join(str(c) for c in mock_console.print.call_args_list)
        assert "No skills found" in printed

    def test_search_fallback_with_results(self):
        """search falls back to plain echo when no Rich console."""
        runner = make_runner()

        with (
            patch_client(search_result=FAKE_SKILLS),
            patch("apm_cli.commands.skills._get_console", return_value=None),
        ):
            result = runner.invoke(skills, ["search", "docker"])

        assert result.exit_code == 0
        assert "docker-expert" in result.output

    def test_search_fallback_no_results(self):
        """search fallback path warns when no results found."""
        runner = make_runner()

        with (
            patch_client(search_result=[]),
            patch("apm_cli.commands.skills._get_console", return_value=None),
        ):
            result = runner.invoke(skills, ["search", "nothing"])

        assert result.exit_code == 0

    def test_search_limit_respected(self):
        """--limit option restricts the number of results returned."""
        runner = make_runner()
        many = [
            {
                "name": f"skill-{i}",
                "skillId": f"skill-{i}",
                "source": "owner/repo",
                "installs": i,
            }
            for i in range(20)
        ]
        mock_console = MagicMock()
        table_instance = MagicMock()

        with (
            patch_client(search_result=many),
            patch("apm_cli.commands.skills._get_console", return_value=mock_console),
            patch("rich.table.Table", return_value=table_instance),
        ):
            result = runner.invoke(skills, ["search", "skill", "--limit", "3"])

        assert result.exit_code == 0
        assert table_instance.add_row.call_count == 3
        row_names = [call.args[0] for call in table_instance.add_row.call_args_list]
        for i in range(3, 20):
            assert f"skill-{i}" not in row_names

    def test_search_network_error_exits_1(self):
        """RequestException causes exit code 1 and renders a hint."""
        import requests as _requests

        runner = make_runner()
        mock_console = MagicMock()

        with (
            patch_client(search_raises=_requests.ConnectionError("boom")),
            patch("apm_cli.commands.skills._get_console", return_value=mock_console),
        ):
            result = runner.invoke(skills, ["search", "docker"])

        assert result.exit_code == 1
        printed = " ".join(str(c) for c in mock_console.print.call_args_list)
        assert "Could not reach skills registry" in printed

    def test_search_generic_exception_exits_1(self):
        """Unexpected exceptions cause exit code 1 (covered by generic handler)."""
        runner = make_runner()
        mock_console = MagicMock()

        with (
            patch(
                "apm_cli.registry.skills_client.SimpleSkillsClient",
                side_effect=RuntimeError("oops"),
            ),
            patch("apm_cli.commands.skills._get_console", return_value=mock_console),
        ):
            result = runner.invoke(skills, ["search", "docker"])

        assert result.exit_code == 1

    def test_search_verbose_flag(self):
        """--verbose flag is accepted without error."""
        runner = make_runner()
        mock_console = MagicMock()

        with (
            patch_client(search_result=FAKE_SKILLS),
            patch("apm_cli.commands.skills._get_console", return_value=mock_console),
            patch("rich.table.Table", MagicMock()),
        ):
            result = runner.invoke(skills, ["search", "docker", "--verbose"])

        assert result.exit_code == 0

    def test_search_long_source_truncated(self):
        """Long source strings are truncated in the Rich path."""
        runner = make_runner()
        long_source = "x" * 200
        items = [{"name": "srv", "skillId": "srv", "source": long_source, "installs": 1}]
        mock_console = MagicMock()
        mock_table = MagicMock()

        with (
            patch_client(search_result=items),
            patch("apm_cli.commands.skills._get_console", return_value=mock_console),
            patch("rich.table.Table", return_value=mock_table),
        ):
            result = runner.invoke(skills, ["search", "srv"])

        assert result.exit_code == 0
        mock_table.add_row.assert_called_once()
        args = mock_table.add_row.call_args[0]
        assert len(args[1]) <= 63  # 60 + "..."

    def test_search_diag_when_env_var_set(self, monkeypatch):
        """When SKILLS_REGISTRY_URL is set, search emits a one-line diagnostic."""
        monkeypatch.setenv("SKILLS_REGISTRY_URL", "https://skills.internal.example.com")
        runner = make_runner()
        mock_console = MagicMock()

        with patch_client(search_result=FAKE_SKILLS) as mock_cls:
            mock_cls.return_value.registry_url = "https://skills.internal.example.com"
            with patch("apm_cli.commands.skills._get_console", return_value=mock_console):
                runner.invoke(skills, ["search", "x"])

        printed = " ".join(str(c) for c in mock_console.print.call_args_list)
        assert "Skills registry:" in printed
        assert ("https", "skills.internal.example.com") in _printed_urls(printed)

    def test_search_no_diag_when_env_var_unset(self, monkeypatch):
        """When SKILLS_REGISTRY_URL is unset, search stays quiet."""
        monkeypatch.delenv("SKILLS_REGISTRY_URL", raising=False)
        runner = make_runner()
        mock_console = MagicMock()

        with patch_client(search_result=FAKE_SKILLS) as mock_cls:
            mock_cls.return_value.registry_url = "https://skills.sh"
            with patch("apm_cli.commands.skills._get_console", return_value=mock_console):
                runner.invoke(skills, ["search", "x"])

        printed = " ".join(str(c) for c in mock_console.print.call_args_list)
        assert "Skills registry:" not in printed


# ---------------------------------------------------------------------------
# skills group help surface
# ---------------------------------------------------------------------------


class TestSkillsGroup:
    def test_skills_help(self):
        runner = make_runner()
        result = runner.invoke(skills, ["--help"])
        assert result.exit_code == 0
        assert "search" in result.output
