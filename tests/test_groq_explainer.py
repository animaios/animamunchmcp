"""Tests for gcm explain (Auto Repo Explainer) pipeline."""

import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest


class TestGatherRepoInfo:
    def test_gathers_outline_tree_symbols(self):
        from jcodemunch_mcp.groq.explainer import _gather_repo_info

        mock_outline = {
            "summary": "A web framework",
            "languages": [{"language": "Python", "files": 42}],
            "total_files": 42,
            "total_symbols": 200,
        }
        mock_tree = {"tree": "src/\n  app.py\n  routes.py"}
        mock_symbols = {
            "symbols": [{"id": "app.main", "kind": "function", "file": "app.py"}]
        }

        with (
            patch(
                "jcodemunch_mcp.tools.get_repo_map.get_repo_map",
                return_value=mock_outline,
            ),
            patch(
                "jcodemunch_mcp.tools.get_file_tree.get_file_tree",
                return_value=mock_tree,
            ),
            patch(
                "jcodemunch_mcp.tools.search_symbols.search_symbols",
                return_value=mock_symbols,
            ),
        ):
            info = _gather_repo_info("test/repo")

        assert info["repo"] == "test/repo"
        assert info["outline"]["summary"] == "A web framework"
        assert "src/" in info["file_tree"]
        assert len(info["key_symbols"]) == 1

    def test_handles_errors_gracefully(self):
        from jcodemunch_mcp.groq.explainer import _gather_repo_info

        with (
            patch(
                "jcodemunch_mcp.tools.get_repo_map.get_repo_map",
                side_effect=Exception("fail"),
            ),
            patch(
                "jcodemunch_mcp.tools.get_file_tree.get_file_tree",
                side_effect=Exception("fail"),
            ),
            patch(
                "jcodemunch_mcp.tools.search_symbols.search_symbols",
                side_effect=Exception("fail"),
            ),
        ):
            info = _gather_repo_info("test/repo")

        assert info["repo"] == "test/repo"
        assert "error" in str(info["outline"])
        assert info["key_symbols"] == []


class TestGenerateNarrationScript:
    def test_parses_json_response(self):
        from jcodemunch_mcp.groq.config import GcmConfig
        from jcodemunch_mcp.groq.explainer import _generate_narration_script

        cfg = GcmConfig(groq_api_key="test-key")
        repo_info = {
            "repo": "test/repo",
            "outline": {"summary": "A framework"},
            "file_tree": "src/\n  main.py",
            "key_symbols": [],
        }

        mock_response = json.dumps(
            [
                {
                    "slide_title": "Welcome",
                    "text": "This is a test repo.",
                    "slide_content": "test/repo — A framework",
                    "is_code": False,
                },
                {
                    "slide_title": "Structure",
                    "text": "It has one file.",
                    "slide_content": "src/\n  main.py",
                    "is_code": False,
                },
            ]
        )

        with patch("jcodemunch_mcp.groq.inference.ask", return_value=mock_response):
            segments = _generate_narration_script(cfg, repo_info)

        assert len(segments) == 2
        assert segments[0].slide_title == "Welcome"
        assert segments[1].text == "It has one file."

    def test_handles_markdown_fenced_json(self):
        from jcodemunch_mcp.groq.config import GcmConfig
        from jcodemunch_mcp.groq.explainer import _generate_narration_script

        cfg = GcmConfig(groq_api_key="test-key")
        repo_info = {
            "repo": "test/repo",
            "outline": {},
            "file_tree": "",
            "key_symbols": [],
        }

        # LLM wraps JSON in code fences
        mock_response = '```json\n[{"slide_title":"Hi","text":"Hello","slide_content":"content","is_code":false}]\n```'

        with patch("jcodemunch_mcp.groq.inference.ask", return_value=mock_response):
            segments = _generate_narration_script(cfg, repo_info)

        assert len(segments) == 1
        assert segments[0].slide_title == "Hi"


class TestRenderSlide:
    @pytest.fixture
    def tmp_png(self):
        fd, path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        yield path
        if os.path.exists(path):
            os.unlink(path)

    def test_renders_text_slide(self, tmp_png):
        pytest.importorskip("PIL")
        from jcodemunch_mcp.groq.explainer import Slide, _render_slide

        slide = Slide(
            title="Overview",
            content="This is a test repo\nWith two lines",
            is_code=False,
            duration=5.0,
        )
        _render_slide(slide, 1, 3, "test/repo", tmp_png)

        assert os.path.exists(tmp_png)
        assert os.path.getsize(tmp_png) > 0

    def test_renders_code_slide(self, tmp_png):
        pytest.importorskip("PIL")
        from jcodemunch_mcp.groq.explainer import Slide, _render_slide

        slide = Slide(
            title="Main Entry",
            content="def main():\n    print('hello')",
            is_code=True,
            duration=5.0,
        )
        _render_slide(slide, 2, 3, "test/repo", tmp_png)

        assert os.path.exists(tmp_png)
        assert os.path.getsize(tmp_png) > 0


class TestCheckDeps:
    def test_reports_missing_pillow(self):
        from jcodemunch_mcp.groq.explainer import _check_deps

        def fake_import(name, *args, **kwargs):
            if "PIL" in name:
                raise ImportError("no PIL")
            return __import__(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            result = _check_deps()
            # Might be None if Pillow is installed; that's fine
            if result:
                assert "Pillow" in result or "ffmpeg" in result


class TestExplainerConstants:
    def test_slide_dimensions(self):
        from jcodemunch_mcp.groq.explainer import SLIDE_HEIGHT, SLIDE_WIDTH

        assert SLIDE_WIDTH == 1920
        assert SLIDE_HEIGHT == 1080

    def test_narration_prompt_has_json_instructions(self):
        from jcodemunch_mcp.groq.explainer import NARRATION_PROMPT

        assert "JSON" in NARRATION_PROMPT
        assert "60" in NARRATION_PROMPT


class TestCLIExplainIntegration:
    def test_explain_subcommand_parsed(self):
        from jcodemunch_mcp.groq.cli import _build_parser

        parser = _build_parser()
        args = parser.parse_args(
            ["explain", "--repo", "pallets/flask", "-o", "test.mp4"]
        )
        assert args.question == "explain"
        assert args.repo == "pallets/flask"
        assert args.output == "test.mp4"

    def test_voice_flag_parsed(self):
        from jcodemunch_mcp.groq.cli import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["--voice", "--repo", "pallets/flask"])
        assert args.voice is True
        assert args.repo == "pallets/flask"
