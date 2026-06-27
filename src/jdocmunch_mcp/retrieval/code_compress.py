"""Lossless-ish code-block compression for fenced blocks (v1.35.0).

Some doc sections devote 60-80% of their bytes to code samples whose
*meaning* survives a comment + blank-line strip. Returning the full
block costs the caller tokens; stripping it costs nothing semantic for
agents that just want to know *what the example does*.

This is opt-in (`compress_code=True` on `get_section` / `get_sections`),
default-off, additive: the original section bytes are never mutated on
disk. Compression operates only on the response copy.

Strategy:

1. Walk the section line-by-line, tracking fenced-code state via the
   same regex shape as the markdown parser (``^(`{3,}|~{3,})``).
2. Inside a fence, detect the language tag from the open delimiter and
   pick the comment marker(s) for that language.
3. Drop blank lines and full-line comments. Preserve indentation,
   strings, and partial-line comments — only the line as a whole is
   dropped when its first non-whitespace token is a comment marker.
4. Outside a fence, lines pass through verbatim.

Block comments (``/* */``, ``<!-- -->``, ``""" """``) are intentionally
NOT stripped — they often carry license headers or behavioral notes
agents should see, and proper handling needs a real lexer.
"""

from __future__ import annotations

import re

# Same fence shape used by parser/markdown_parser.py — open delimiter
# captures the run length so the close must match.
_FENCE_RE = re.compile(r"^(?P<fence>`{3,}|~{3,})\s*(?P<lang>[\w+\-#.]*)\s*$")

# Per-language line-comment markers. Conservative — when in doubt, no
# strip (the line stays).
_HASH_LANGS = {
    "python", "py", "py3", "py2", "ipython",
    "sh", "bash", "zsh", "shell", "console", "fish",
    "yaml", "yml", "toml", "ini", "conf", "cfg",
    "ruby", "rb", "rake", "gemspec",
    "r", "perl", "pl", "elixir", "ex", "exs", "crystal", "cr",
    "dockerfile", "docker", "makefile", "make", "cmake",
    "tcl", "awk", "powershell", "ps1", "ps",
    "nim", "julia", "jl",
}
_DOUBLE_SLASH_LANGS = {
    "javascript", "js", "jsx",
    "typescript", "ts", "tsx",
    "java", "kotlin", "kt", "scala", "groovy", "gradle",
    "c", "cpp", "c++", "cxx", "h", "hpp", "objective-c", "objc",
    "csharp", "cs", "fsharp", "fs",
    "go", "golang", "rust", "rs", "swift", "dart",
    "php", "d", "zig", "v", "vlang",
    "solidity", "sol", "verilog", "systemverilog", "sv",
}
_DASH_DASH_LANGS = {"sql", "lua", "haskell", "hs", "ada", "eiffel", "vhdl"}
_SEMICOLON_LANGS = {
    "lisp", "scheme", "clojure", "clj", "cljs", "elisp", "el",
    "racket", "rkt", "common-lisp",
    "asm", "assembly", "nasm",
}
_PERCENT_LANGS = {"erlang", "erl", "prolog", "matlab", "octave", "tex", "latex"}
_REM_LANGS: set[str] = set()


def _markers_for(lang: str) -> tuple[str, ...]:
    """Return the comment-prefix tuple for a fence language tag."""
    lang = (lang or "").strip().lower()
    if lang in _HASH_LANGS:
        return ("#",)
    if lang in _DOUBLE_SLASH_LANGS:
        return ("//",)
    if lang in _DASH_DASH_LANGS:
        return ("--",)
    if lang in _SEMICOLON_LANGS:
        return (";",)
    if lang in _PERCENT_LANGS:
        return ("%",)
    return ()


def _is_comment_line(line: str, markers: tuple[str, ...]) -> bool:
    """True when the line's first non-whitespace token is a comment."""
    stripped = line.lstrip()
    if not stripped:
        return False
    return any(stripped.startswith(m) for m in markers)


def compress_fenced_code(content: str) -> tuple[str, int]:
    """Strip blank lines + line comments from fenced code blocks.

    Returns ``(compressed_content, bytes_saved)``. Bytes saved is
    measured against UTF-8 encoding. Outside-fence content is passed
    through unchanged.
    """
    if not content:
        return content, 0

    lines = content.splitlines(keepends=True)
    out: list[str] = []
    in_fence = False
    fence_close: str | None = None
    markers: tuple[str, ...] = ()

    for line in lines:
        bare = line.rstrip("\r\n")
        if not in_fence:
            m = _FENCE_RE.match(bare)
            if m:
                in_fence = True
                fence_close = m.group("fence")
                markers = _markers_for(m.group("lang"))
                out.append(line)
                continue
            out.append(line)
            continue

        # Inside a fence — first check for matching close.
        if fence_close and bare.startswith(fence_close):
            close_run = re.match(rf"^{re.escape(fence_close[0])}{{{len(fence_close)},}}\s*$", bare)
            if close_run:
                in_fence = False
                fence_close = None
                markers = ()
                out.append(line)
                continue

        # Drop blank lines and pure-comment lines.
        if not bare.strip():
            continue
        if markers and _is_comment_line(bare, markers):
            continue
        out.append(line)

    compressed = "".join(out)
    saved = len(content.encode("utf-8")) - len(compressed.encode("utf-8"))
    return compressed, max(0, saved)
