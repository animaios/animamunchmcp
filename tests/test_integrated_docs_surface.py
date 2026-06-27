"""Integrated jDocMunch reading-surface smoke tests."""

from __future__ import annotations


READING_TOOLS = [
    "index_content",
    "list_content",
    "get_outline",
    "get_file",
    "search_units",
    "get_unit",
    "get_unit_context",
]


def test_reading_surface_exposes_only_unified_tools(monkeypatch):
    from jcodemunch_mcp.server import _build_tools_list

    monkeypatch.setenv("JCODEMUNCH_TOOL_SURFACE", "reading")
    names = [t.name for t in _build_tools_list()]

    assert names == READING_TOOLS
    assert "search_symbols" not in names
    assert "search_sections" not in names


def test_reading_surface_can_be_selected_from_config(monkeypatch):
    from jcodemunch_mcp import config as config_module
    from jcodemunch_mcp.server import _build_tools_list

    monkeypatch.delenv("JCODEMUNCH_TOOL_SURFACE", raising=False)
    monkeypatch.setitem(config_module._GLOBAL_CONFIG, "tool_surface", "jmri")
    names = [t.name for t in _build_tools_list()]

    assert names == READING_TOOLS
    assert "search_symbols" not in names
    assert "search_sections" not in names


def test_index_content_searches_code_and_docs(tmp_path):
    from jcodemunch_mcp.tools.content_router import (
        get_file,
        get_outline,
        index_content,
        search_units,
    )

    repo = tmp_path / "repo"
    docs = repo / "docs"
    docs.mkdir(parents=True)
    (repo / "app.py").write_text(
        "def hello(name):\n    return f'hi {name}'\n",
        encoding="utf-8",
    )
    (docs / "guide.md").write_text(
        "# Guide\n\nUse `hello` to greet people.\n\n## Install\n\nRun it locally.\n",
        encoding="utf-8",
    )

    code_store = str(tmp_path / "code-index")
    doc_store = str(tmp_path / "doc-index")
    indexed = index_content(
        path=str(repo),
        domain="both",
        use_ai_summaries=False,
        use_embeddings=False,
        storage_path=code_store,
        doc_storage_path=doc_store,
    )

    assert indexed["result_count"] == 2
    code_repo = indexed["results"][0]["result"]["repo"]
    doc_repo = indexed["results"][1]["result"]["repo"]
    assert code_repo == doc_repo

    code_search = search_units(
        repo=code_repo,
        query="hello",
        domain="code",
        storage_path=code_store,
    )
    assert code_search["results"][0]["domain"] == "code"
    assert code_search["results"][0]["result"]["results"]

    doc_search = search_units(
        repo=doc_repo,
        query="greet",
        domain="docs",
        doc_storage_path=doc_store,
    )
    assert doc_search["results"][0]["domain"] == "docs"
    assert doc_search["results"][0]["result"]["results"]

    outline = get_outline(
        repo=doc_repo,
        file_path="docs/guide.md",
        domain="docs",
        doc_storage_path=doc_store,
    )
    assert outline["results"][0]["unit_type"] == "section"
    assert outline["results"][0]["result"]["section_count"] >= 1

    doc_file = get_file(
        repo=code_repo,
        file_path="docs/guide.md",
        domain="auto",
        start_line=1,
        end_line=1,
        storage_path=code_store,
        doc_storage_path=doc_store,
    )
    assert doc_file["results"][0]["result"]["content"] == "# Guide"

    code_file = get_file(
        repo=doc_repo,
        file_path="app.py",
        domain="auto",
        storage_path=code_store,
        doc_storage_path=doc_store,
    )
    assert code_file["results"][0]["domain"] == "code"
    assert "def hello" in code_file["results"][0]["result"]["content"]


def test_explicit_doc_name_still_auto_routes_by_source_root(tmp_path):
    from jcodemunch_mcp.tools.content_router import get_file, get_outline, index_content

    repo = tmp_path / "repo"
    docs = repo / "docs"
    docs.mkdir(parents=True)
    (repo / "app.py").write_text(
        "def hello(name):\n    return f'hi {name}'\n",
        encoding="utf-8",
    )
    (docs / "guide.md").write_text(
        "# Guide\n\nUse `hello` to greet people.\n",
        encoding="utf-8",
    )

    code_store = str(tmp_path / "code-index")
    doc_store = str(tmp_path / "doc-index")
    indexed = index_content(
        path=str(repo),
        domain="both",
        name="manual-docs",
        use_ai_summaries=False,
        use_embeddings=False,
        storage_path=code_store,
        doc_storage_path=doc_store,
    )

    code_repo = indexed["results"][0]["result"]["repo"]
    doc_repo = indexed["results"][1]["result"]["repo"]
    assert code_repo != doc_repo

    doc_outline = get_outline(
        repo=code_repo,
        file_path="docs/guide.md",
        domain="auto",
        storage_path=code_store,
        doc_storage_path=doc_store,
    )
    assert doc_outline["results"][0]["domain"] == "docs"
    assert doc_outline["results"][0]["result"]["section_count"] >= 1

    code_file = get_file(
        repo=doc_repo,
        file_path="app.py",
        domain="auto",
        storage_path=code_store,
        doc_storage_path=doc_store,
    )
    assert code_file["results"][0]["domain"] == "code"
    assert "def hello" in code_file["results"][0]["result"]["content"]
