"""Tests for CLAUDE.md policy update (Feature 9: Session-Aware Routing)."""

import pytest

# All required terms that must be present in _CLAUDE_MD_POLICY
REQUIRED_POLICY_TERMS = [
    ("assemble_task_context", "assemble_task_context single-call orchestrator"),
    ("negative_evidence", "negative_evidence for empty results"),
    ("high", "high confidence level"),
    ("medium", "medium confidence level"),
    ("low", "low confidence level"),
    ("register_edit", "register_edit for bulk cache invalidation"),
    ("Session-Aware Routing", "Session-Aware Routing section header"),
    ("budget_warning", "budget_warning handling"),
    ("No existing implementation", "no implementation guidance"),
    ("Opening move", "opening move instruction"),
]


class TestClaudeMdPolicyContent:
    """Tests for updated _CLAUDE_MD_POLICY constant."""

    @pytest.mark.parametrize("term,description", REQUIRED_POLICY_TERMS)
    def test_policy_contains_required_term(self, term, description):
        """_CLAUDE_MD_POLICY must contain all required terms."""
        from jcodemunch_mcp.cli.init import _CLAUDE_MD_POLICY

        assert term in _CLAUDE_MD_POLICY, f"Missing {description}"


class TestCursorRulesContent:
    """Tests for updated _CURSOR_RULES_CONTENT."""

    def test_cursor_rules_inherits_policy(self):
        """_CURSOR_RULES_CONTENT must include the updated policy."""
        from jcodemunch_mcp.cli.init import _CLAUDE_MD_POLICY, _CURSOR_RULES_CONTENT

        # Cursor rules should contain the policy text
        assert _CLAUDE_MD_POLICY in _CURSOR_RULES_CONTENT

    def test_cursor_rules_has_frontmatter(self):
        """_CURSOR_RULES_CONTENT must have MDC frontmatter."""
        from jcodemunch_mcp.cli.init import _CURSOR_RULES_CONTENT

        assert "---" in _CURSOR_RULES_CONTENT
        assert "description:" in _CURSOR_RULES_CONTENT


class TestWindsurfRulesContent:
    """Tests for updated _WINDSURF_RULES_CONTENT."""

    def test_windsurf_rules_equals_policy(self):
        """_WINDSURF_RULES_CONTENT should equal _CLAUDE_MD_POLICY."""
        from jcodemunch_mcp.cli.init import _CLAUDE_MD_POLICY, _WINDSURF_RULES_CONTENT

        assert _WINDSURF_RULES_CONTENT == _CLAUDE_MD_POLICY


class TestInstallClaudeMdIntegration:
    """Integration tests for install_claude_md with updated policy."""

    def test_install_claude_md_contains_new_policy(self, tmp_path, monkeypatch):
        """Installed CLAUDE.md must contain Session-Aware Routing section."""
        from jcodemunch_mcp.cli.init import install_claude_md

        monkeypatch.setattr(
            "jcodemunch_mcp.cli.init._claude_md_path",
            lambda scope: tmp_path / "CLAUDE.md",
        )
        install_claude_md("project", backup=False)
        content = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
        assert "Session-Aware Routing" in content


class TestFilterPolicyForTools:
    """Tests for _filter_policy_for_tools (issue #242 follow-up)."""

    def test_none_returns_unchanged(self):
        """None active_tools means full profile — no filtering."""
        from jcodemunch_mcp.cli.init import _CLAUDE_MD_POLICY, _filter_policy_for_tools

        assert _filter_policy_for_tools(_CLAUDE_MD_POLICY, None) == _CLAUDE_MD_POLICY

    def test_disabled_tools_respected(self):
        """Individually disabled tools should be filtered out."""
        from jcodemunch_mcp.cli.init import _CLAUDE_MD_POLICY, _filter_policy_for_tools
        from jcodemunch_mcp.server import _CANONICAL_TOOL_NAMES

        active = set(_CANONICAL_TOOL_NAMES) - {"search_text"}
        result = _filter_policy_for_tools(_CLAUDE_MD_POLICY, active)
        assert "search_text" not in result
        assert "search_symbols" in result  # other tools unaffected
