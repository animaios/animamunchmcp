#!/usr/bin/env python3
"""
Dead code audit script for jcodemunch-mcp.
Extracts all function/method definitions and counts references across the codebase.
Reports functions with only 1 reference (the definition itself) as potentially dead.
"""

import ast
import os
import re
import sys
import json
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Set, Tuple

SRC_DIR = Path("src/jcodemunch_mcp")

# Patterns that indicate a function is NOT dead even if it has few references
# (called dynamically, via reflection, CLI dispatch, etc.)
NON_DEAD_PATTERNS = [
    # CLI dispatches
    r"known_commands",
    r"subparsers",
    r"add_parser",
    r"set_defaults",
    # Server tool registration
    r"@mcp\.tool",
    r"@mcp\.prompt",
    r"@mcp\.resource",
    r"register_tool",
    r"_build_tools_list",
    r"call_tool",
    # Class method overrides / ABC
    r"__init__",
    r"__post_init__",
    r"__enter__",
    r"__exit__",
    r"__call__",
    r"__len__",
    r"__getitem__",
    r"__setitem__",
    r"__delitem__",
    r"__iter__",
    r"__next__",
    r"__repr__",
    r"__str__",
    r"__eq__",
    r"__hash__",
    r"__bool__",
    r"__contains__",
    # Common dynamic dispatch patterns
    r"getattr\(",
    r"hasattr\(",
    r"setattr\(",
    r"globals\(\)",
    r"locals\(\)",
    r"__subclasses__",
    r"__init_subclass__",
    r"__class_getitem__",
    r"__subclasshook__",
    r"__subclasses__",
    r"import_module",
    r"importlib",
    r"pkgutil",
    r"runpy",
    # Test fixtures / setup
    r"setUp",
    r"tearDown",
    r"setUpClass",
    r"tearDownClass",
    r"fixture",
    r"conftest",
    # Hook handlers
    r"hook_",
    r"_handler",
    # Protocol / ABC methods
    r"abstractmethod",
    # CLI entry points
    r"main",
    r"run_",
    r"cli",
    # Signal handlers, callbacks
    r"signal",
    r"callback",
    # Worker / daemon entry points
    r"worker",
    r"daemon",
    r"target=",
    r"Thread\(",
]

# Names that are commonly called dynamically or via string dispatch
DYNAMIC_DISPATCH_MODULES = {
    "server.py", "config.py", "cli/__init__.py", "main.py"
}

def extract_definitions(filepath: Path) -> List[Tuple[str, str, int, str]]:
    """
    Extract all function/method definitions from a Python file.
    Returns: [(qualified_name, short_name, line_number, context), ...]
    """
    definitions = []
    try:
        content = filepath.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return definitions

    try:
        tree = ast.parse(content, filename=str(filepath))
    except SyntaxError:
        return definitions

    # Track class context
    class_stack: List[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            class_stack.append(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Build qualified name
            # Find enclosing class by looking at parent (not directly available in walk)
            # We'll use a simpler approach: track class defs in walk order
            func_name = node.name
            # Try to find parent class by checking if this node is in the class body
            parent_class = _find_parent_class(tree, node, class_stack)
            if parent_class:
                qual_name = f"{parent_class}.{func_name}"
            else:
                qual_name = func_name

            # Determine context
            ctx = _get_context(node, content)

            definitions.append((qual_name, func_name, node.lineno, ctx))

            # If this is a class def, push to stack for children
            # (already handled above by the ClassDef branch)

    return definitions


def _find_parent_class(tree, func_node, class_stack):
    """Find the enclosing class for a function node."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if item is func_node:
                    return node.name
    return None


def _get_context(node, content):
    """Get a brief context string for a function definition."""
    lines = content.split("\n")
    if node.lineno <= len(lines):
        line = lines[node.lineno - 1].strip()
        return line[:120]
    return ""


def extract_references(filepath: Path) -> Dict[str, int]:
    """
    Extract all function/method references from a Python file.
    Returns: {name: count, ...}
    """
    refs = defaultdict(int)
    try:
        content = filepath.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return refs

    try:
        tree = ast.parse(content, str(filepath))
    except SyntaxError:
        return refs

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _get_call_name(node)
            if name:
                refs[name] += 1
        elif isinstance(node, ast.Attribute):
            # Method references like obj.method
            if node.attr:
                refs[node.attr] += 1
        elif isinstance(node, ast.Name):
            if isinstance(node.ctx, (ast.Load, ast.Del)):
                refs[node.id] += 1

    return refs


def _get_call_name(node):
    """Get the name of a called function from a Call node."""
    if isinstance(node.func, ast.Name):
        return node.func.id
    elif isinstance(node.func, ast.Attribute):
        return node.func.attr
    elif isinstance(node.func, ast.Subscript):
        return _get_call_name_from_subscript(node.func)
    return None


def _get_call_name_from_subscript(node):
    """Get the name from a subscript call like obj[method]()."""
    if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
        return node.slice.value
    return None


def find_all_definitions(src_dir: Path) -> List[Tuple[Path, str, str, int, str]]:
    """Find all function/method definitions in the source tree."""
    all_defs = []
    for py_file in src_dir.rglob("*.py"):
        defs = extract_definitions(py_file)
        for qual_name, short_name, lineno, ctx in defs:
            all_defs.append((py_file, qual_name, short_name, lineno, ctx))
    return all_defs


def find_all_references(src_dir: Path) -> Dict[str, int]:
    """Find all function/method references across the source tree."""
    all_refs = defaultdict(int)
    for py_file in src_dir.rglob("*.py"):
        refs = extract_references(py_file)
        for name, count in refs.items():
            all_refs[name] += count
    return dict(all_refs)


def is_likely_dead_code(qual_name, short_name, lineno, ctx, file_path,
                         all_refs, all_defs_by_name):
    """
    Determine if a function is likely dead code based on reference count and patterns.
    Returns: (is_dead, confidence, reason)
    """
    # Count references to short name and qualified name
    short_refs = all_refs.get(short_name, 0)
    qual_refs = all_refs.get(qual_name, 0)
    total_refs = max(short_refs, qual_refs)

    # Subtract the definition itself
    if total_refs <= 1:
        # Only the definition exists - definitely dead unless it's a special pattern
        # Check if it matches non-dead patterns
        for pattern in NON_DEAD_PATTERNS:
            if re.search(pattern, ctx) or re.search(pattern, short_name):
                return False, "low", f"Matches non-dead pattern: {pattern}"
            # Also check the function body for dynamic dispatch hints
        return True, "high", f"No references found (short={short_refs}, qual={qual_refs})"

    # For functions with few references (2-3), check if they might be called dynamically
    if total_refs <= 3:
        # Check if it's a method override or protocol method
        if short_name.startswith("_") and not short_name.startswith("__"):
            # Private methods - might be called dynamically
            return False, "medium", f"Private method with {total_refs} refs - could be dynamic dispatch"

        # Check if context suggests dynamic dispatch
        dynamic_hints = ["dispatch", "callback", "handler", "hook", "signal",
                         "register", "plugin", "strategy", "factory"]
        for hint in dynamic_hints:
            if hint in ctx.lower() or hint in qual_name.lower():
                return False, "medium", f"Possible dynamic dispatch (hint: {hint})"

    # Check if called via getattr/hasattr
    for pattern in [r"getattr\(", r"hasattr\(", r"setattr\(", r"globals\(\)"]:
        if re.search(pattern, ctx):
            return False, "medium", "Possible dynamic dispatch via reflection"

    # Check if the module is a known dynamic dispatch module
    module = file_path.name if file_path else ""
    if module in DYNAMIC_DISPATCH_MODULES:
        return False, "medium", f"Module {module} uses dynamic dispatch"

    return False, "low", f"Has {total_refs} references"


def check_test_usage(short_name, test_dir: Path = Path("tests")):
    """Check if a function is used in tests."""
    if not test_dir.exists():
        return False
    for test_file in test_dir.rglob("*.py"):
        try:
            content = test_file.read_text(encoding="utf-8", errors="ignore")
            if short_name in content:
                return True
        except Exception:
            continue
    return False


def check_string_usage(short_name, src_dir: Path):
    """Check if a function name appears in strings (dynamic dispatch)."""
    for py_file in src_dir.rglob("*.py"):
        try:
            content = py_file.read_text(encoding="utf-8", errors="ignore")
            # Look for string references like "func_name" or 'func_name'
            if f'"{short_name}"' in content or f"'{short_name}'" in content:
                return True
        except Exception:
            continue
    return False


def main():
    src_dir = SRC_DIR

    print("=" * 80)
    print("DEAD CODE AUDIT - jcodemunch-mcp")
    print("=" * 80)
    print()

    # Phase 1: Extract all definitions
    print("[1/4] Extracting function/method definitions...")
    all_defs = find_all_definitions(src_dir)
    print(f"  Found {len(all_defs)} definitions")

    # Phase 2: Extract all references
    print("[2/4] Extracting references...")
    all_refs = find_all_references(src_dir)
    print(f"  Found {len(all_refs)} unique referenced names")

    # Phase 3: Analyze each definition
    print("[3/4] Analyzing definitions for dead code...")
    dead_code_candidates = []

    for file_path, qual_name, short_name, lineno, ctx in all_defs:
        is_dead, confidence, reason = is_likely_dead_code(
            qual_name, short_name, lineno, ctx, file_path,
            all_refs, {}
        )

        if is_dead:
            # Also check if used in tests or strings
            in_tests = check_test_usage(short_name)
            in_strings = check_string_usage(short_name, src_dir)

            if in_tests:
                confidence = "low (test usage)"
                reason += " [USED IN TESTS]"
            elif in_strings:
                confidence = "low (string ref)"
                reason += " [USED IN STRINGS]"

            dead_code_candidates.append({
                "file": str(file_path),
                "qual_name": qual_name,
                "short_name": short_name,
                "line": lineno,
                "context": ctx,
                "confidence": confidence,
                "reason": reason,
                "in_tests": in_tests,
                "in_strings": in_strings,
            })

    # Phase 4: Generate report
    print("[4/4] Generating report...")
    print()

    # Sort by confidence (high first) then by file
    confidence_order = {"high": 0, "medium": 1, "low": 2}
    dead_code_candidates.sort(
        key=lambda x: (confidence_order.get(x["confidence"].split(" ")[0], 99), x["file"], x["line"])
    )

    # Print summary
    high = [c for c in dead_code_candidates if c["confidence"].startswith("high")]
    medium = [c for c in dead_code_candidates if c["confidence"].startswith("medium")]
    low = [c for c in dead_code_candidates if c["confidence"].startswith("low")]

    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"Total definitions analyzed: {len(all_defs)}")
    print(f"Dead code candidates: {len(dead_code_candidates)}")
    print(f"  HIGH confidence:   {len(high)}")
    print(f"  MEDIUM confidence: {len(medium)}")
    print(f"  LOW confidence:    {len(low)}")
    print()

    # Print HIGH confidence findings
    if high:
        print("=" * 80)
        print("HIGH CONFIDENCE DEAD CODE (definitely dead)")
        print("=" * 80)
        for i, c in enumerate(high, 1):
            print(f"\n{i}. {c['qual_name']}")
            print(f"   File: {c['file']}:{c['line']}")
            print(f"   Context: {c['context']}")
            print(f"   Reason: {c['reason']}")

    # Print MEDIUM confidence findings
    if medium:
        print()
        print("=" * 80)
        print("MEDIUM CONFIDENCE DEAD CODE (probably dead)")
        print("=" * 80)
        for i, c in enumerate(medium, 1):
            print(f"\n{i}. {c['qual_name']}")
            print(f"   File: {c['file']}:{c['line']}")
            print(f"   Context: {c['context']}")
            print(f"   Reason: {c['reason']}")

    # Print LOW confidence findings (only first 50 to avoid noise)
    if low:
        print()
        print("=" * 80)
        print("LOW CONFIDENCE DEAD CODE (possibly dead, needs manual review)")
        print("=" * 80)
        for i, c in enumerate(low[:50], 1):
            print(f"\n{i}. {c['qual_name']}")
            print(f"   File: {c['file']}:{c['line']}")
            print(f"   Context: {c['context']}")
            print(f"   Reason: {c['reason']}")
        if len(low) > 50:
            print(f"\n... and {len(low) - 50} more low-confidence candidates")

    # Save full results to JSON
    output = {
        "summary": {
            "total_definitions": len(all_defs),
            "dead_code_candidates": len(dead_code_candidates),
            "high_confidence": len(high),
            "medium_confidence": len(medium),
            "low_confidence": len(low),
        },
        "high_confidence": high,
        "medium_confidence": medium,
        "low_confidence": low,
    }

    output_path = Path("dead_code_audit_results.json")
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)
    print()
    print(f"Full results saved to: {output_path}")


if __name__ == "__main__":
    main()
