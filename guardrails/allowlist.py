"""
Allowlist policy: what the agent is permitted to do, enforced
identically in both discovery (agent/) and replay (replay/) so a
capability can't be discovered against, or later replayed against,
anything outside the declared boundary.

Per section 3.4: "Enforce an explicit, configurable allowlist (e.g.
permitted domains/routes, and which action types are allowed). The
agent must not act outside it."

This is deliberately a static, file-based config -- not something the
agent or a calling capability can widen at runtime. Widening the
allowlist is a human decision (edit this file / the JSON it loads),
never something either the LLM-driven discovery loop or a replay
call can do to itself.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field

DEFAULT_POLICY_PATH = Path(__file__).resolve().parent / "policy.json"


class AllowlistPolicy(BaseModel):
    # Domains the agent may navigate to at all, e.g. "127.0.0.1:5000".
    # Exact match on host:port -- deliberately not wildcarded, since
    # the whole point is a tight boundary around one known target.
    allowed_domains: list[str] = Field(default_factory=list)

    # URL path patterns (fnmatch-style, e.g. "/member/*") the agent
    # may navigate to within an allowed domain. Empty list means "any
    # path on an allowed domain" -- see is_route_allowed.
    allowed_route_patterns: list[str] = Field(default_factory=list)

    # Action types permitted at all, matching schemas.artifact.ActionType
    # values plus the discovery-only ones. Anything not listed here is
    # refused regardless of domain/route.
    allowed_action_types: list[str] = Field(default_factory=list)

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "AllowlistPolicy":
        path = path or DEFAULT_POLICY_PATH
        if not path.exists():
            raise FileNotFoundError(
                f"No allowlist policy found at {path}. Refusing to run without an "
                f"explicit policy -- see guardrails/policy.json for the expected shape."
            )
        return cls.model_validate_json(path.read_text())

    def is_domain_allowed(self, url: str) -> bool:
        host = urlparse(url).netloc
        return host in self.allowed_domains

    def is_route_allowed(self, url: str) -> bool:
        if not self.allowed_route_patterns:
            return True  # no route restriction beyond domain
        path = urlparse(url).path or "/"
        return any(fnmatch.fnmatch(path, pattern) for pattern in self.allowed_route_patterns)

    def is_action_type_allowed(self, action_type: str) -> bool:
        return action_type in self.allowed_action_types

    def check_navigation(self, url: str) -> None:
        """Raise PolicyViolation if `url` is outside the allowlist.
        Called before every navigate action, in both discovery and
        replay -- see guardrails/enforcement.py."""
        if not self.is_domain_allowed(url):
            raise PolicyViolation(
                f"Navigation to '{url}' blocked: domain not in allowlist "
                f"({self.allowed_domains})."
            )
        if not self.is_route_allowed(url):
            raise PolicyViolation(
                f"Navigation to '{url}' blocked: path not in allowed_route_patterns "
                f"({self.allowed_route_patterns})."
            )

    def check_action_type(self, action_type: str) -> None:
        if not self.is_action_type_allowed(action_type):
            raise PolicyViolation(
                f"Action type '{action_type}' blocked: not in allowlist "
                f"({self.allowed_action_types})."
            )


class PolicyViolation(Exception):
    """Raised when an action or navigation falls outside the allowlist.
    Always treated as a hard failure by both discovery and replay --
    never silently skipped, never downgraded to a warning."""
    pass