"""Simple skills.sh client for skill discovery.

Mirrors the design of :mod:`apm_cli.registry.client` (the MCP registry
client): explicit timeouts, env-var override, optional HTTP cache, and
URL validation.  Kept as a separate module so the two registries can
evolve independently and so callers (CLI command, future install path)
import a single, well-scoped surface.
"""

import logging
import os
from typing import Any  # noqa: F401, UP035
from urllib.parse import urlparse

import requests

_log = logging.getLogger(__name__)


_DEFAULT_REGISTRY_URL = "https://skills.sh"

# Timeouts mirror the MCP client: a tight connect bound so a typo in
# SKILLS_REGISTRY_URL fails fast in CI rather than hanging, and a
# generous read bound for slow registries / proxies. Both override-able
# via env for enterprise tuning.
_DEFAULT_CONNECT_TIMEOUT = 10.0
_DEFAULT_READ_TIMEOUT = 30.0


def _safe_headers(response) -> dict[str, str]:
    """Return response headers as a plain dict, tolerating Mock objects in tests."""
    try:
        return dict(response.headers)
    except (TypeError, AttributeError):
        return {}


def _resolve_timeout() -> tuple:
    """Return the ``(connect, read)`` timeout tuple for skills HTTP calls."""

    def _read_float(env_key: str, default: float) -> float:
        raw = os.environ.get(env_key)
        if not raw:
            return default
        try:
            value = float(raw)
            if value <= 0:
                return default
            return value
        except (TypeError, ValueError):
            return default

    return (
        _read_float("SKILLS_REGISTRY_CONNECT_TIMEOUT", _DEFAULT_CONNECT_TIMEOUT),
        _read_float("SKILLS_REGISTRY_READ_TIMEOUT", _DEFAULT_READ_TIMEOUT),
    )


class SimpleSkillsClient:
    """Client for querying skills.sh for skill discovery.

    Currently exposes a search API; the upstream service has no
    documented per-skill detail endpoint, so :meth:`get_skill_url` is
    provided as a convenience for callers that need to direct a user
    to the rendered listing page.
    """

    def __init__(self, registry_url: str | None = None):
        """Initialize the skills registry client.

        Args:
            registry_url: Base URL for the skills registry. Falls back to
                ``SKILLS_REGISTRY_URL`` env var, then to the public default.

        Raises:
            ValueError: If the resolved URL is malformed, uses an unsupported
                scheme, or uses ``http://`` without
                ``SKILLS_REGISTRY_ALLOW_HTTP=1`` opt-in.
        """
        env_override = os.environ.get("SKILLS_REGISTRY_URL")
        if env_override is not None and env_override.strip() == "":
            env_override = None

        resolved = registry_url or env_override or _DEFAULT_REGISTRY_URL
        resolved = resolved.strip().rstrip("/")

        parsed = urlparse(resolved)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError(
                f"Invalid skills registry URL {resolved!r}: expected scheme://host "
                f"(e.g. https://skills.sh). Check SKILLS_REGISTRY_URL if set."
            )
        if parsed.scheme not in ("http", "https"):
            raise ValueError(
                f"Unsupported scheme {parsed.scheme!r} in skills registry URL "
                f"{resolved!r}: only https:// is supported (http:// requires "
                f"SKILLS_REGISTRY_ALLOW_HTTP=1). Check SKILLS_REGISTRY_URL if set."
            )
        if parsed.scheme == "http" and not os.environ.get("SKILLS_REGISTRY_ALLOW_HTTP"):
            raise ValueError(
                f"Insecure skills registry URL {resolved!r}: http:// is not allowed "
                f"by default. Set SKILLS_REGISTRY_ALLOW_HTTP=1 to opt in to plaintext "
                f"HTTP (not recommended for production). "
                f"Check SKILLS_REGISTRY_URL if set."
            )

        self.registry_url = resolved
        self._is_custom_url = registry_url is not None or env_override is not None
        self.session = requests.Session()
        self._timeout = _resolve_timeout()
        self._http_cache = self._init_http_cache()

    @staticmethod
    def _init_http_cache():
        """Resolve the shared HTTP response cache, or ``None`` if disabled."""
        if os.environ.get("APM_NO_CACHE", "").strip() in ("1", "true", "yes"):
            return None
        try:
            from apm_cli.cache import HttpCache, get_cache_root

            return HttpCache(get_cache_root())
        except Exception as exc:  # pragma: no cover - defensive
            _log.debug("HTTP cache unavailable, falling back to network: %s", exc)
            return None

    def _cached_get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, dict[str, str]]:
        """GET ``url`` honoring the persistent HTTP cache.

        Same shape as ``SimpleRegistryClient._cached_get_json``: fresh
        cache hits short-circuit; stale entries revalidate via
        ``If-None-Match``; auth-bearing requests bypass cache to avoid
        cross-identity body leakage.
        """
        cache_key = url
        if params:
            from urllib.parse import urlencode

            cache_key = f"{url}?{urlencode(sorted(params.items()))}"

        session_auth = bool(self.session.headers.get("Authorization"))
        if session_auth or self._http_cache is None:
            kwargs0: dict[str, Any] = {"timeout": self._timeout}
            if params:
                kwargs0["params"] = params
            response = self.session.get(url, **kwargs0)
            response.raise_for_status()
            return response.json(), _safe_headers(response)

        cached = self._http_cache.get(cache_key)
        if cached is not None:
            try:
                import json as _json

                return _json.loads(cached.body.decode("utf-8")), {}
            except (ValueError, UnicodeDecodeError):
                pass  # fall through to network

        request_headers = self._http_cache.conditional_headers(cache_key)
        kwargs: dict[str, Any] = {"timeout": self._timeout}
        if params:
            kwargs["params"] = params
        if request_headers:
            kwargs["headers"] = request_headers
        response = self.session.get(url, **kwargs)

        if response.status_code == 304:
            self._http_cache.refresh_expiry(cache_key, _safe_headers(response))
            cached = self._http_cache.get(cache_key)
            if cached is not None:
                try:
                    import json as _json

                    return _json.loads(cached.body.decode("utf-8")), _safe_headers(response)
                except (ValueError, UnicodeDecodeError):
                    pass
            kwargs2: dict[str, Any] = {"timeout": self._timeout}
            if params:
                kwargs2["params"] = params
            response = self.session.get(url, **kwargs2)

        response.raise_for_status()
        try:
            body = response.content
            self._http_cache.store(
                cache_key,
                body,
                status_code=response.status_code,
                headers=_safe_headers(response),
            )
        except Exception as exc:  # pragma: no cover - defensive
            _log.debug("HTTP cache store failed for %s: %s", cache_key, exc)
        return response.json(), _safe_headers(response)

    def search_skills(self, query: str) -> list[dict[str, Any]]:
        """Search skills.sh for skills matching ``query``.

        Args:
            query: Free-text search string. Required by the upstream API
                (an empty query returns HTTP 400).

        Returns:
            List of skill dicts. Each dict contains keys returned by
            skills.sh, including ``id``, ``skillId``, ``name``,
            ``installs``, and ``source`` (the GitHub ``owner/repo``
            providing the skill).

        Raises:
            ValueError: If ``query`` is empty.
            requests.RequestException: If the request fails.
        """
        if not query or not query.strip():
            raise ValueError("query must be a non-empty string")

        url = f"{self.registry_url}/api/search"
        data, _hdrs = self._cached_get_json(url, params={"q": query.strip()})
        data = data or {}
        skills = data.get("skills", [])
        # Defensive: upstream returns a list, but absorb a single-dict
        # response shape rather than crashing the CLI.
        if isinstance(skills, dict):
            skills = [skills]
        return list(skills) if isinstance(skills, list) else []

    def get_skill_url(self, skill: dict[str, Any]) -> str | None:
        """Return the public skills.sh page URL for a result entry.

        Used by the CLI to point users at the upstream listing for
        installation instructions, since skills.sh has no documented
        per-skill detail JSON endpoint.
        """
        source = skill.get("source")
        slug = skill.get("skillId") or skill.get("name")
        if not source or not slug:
            return None
        return f"{self.registry_url}/{source}/{slug}"
