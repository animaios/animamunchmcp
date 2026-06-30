# animamunchmcp — Dual SOP (jcm + Serena)

> **Maintenance note:** This file is the binding SOP for agentic work in this repo. When jcm releases a new tool (tracked in `CLAUDE.md` release notes), or Serena ships a new tool upstream (inventory in `serena/src/serena/tools/*`), the matching section below must be updated.

## 0. Absolute token-efficiency commitment

The agent MUST prefer a **structured MCP call** (`jcm`) OR a **symbolic LSP call** (`Serena`) for every code operation. Native `Read` / `Grep` / `Glob` / `Bash` are reserved for:

- Precondition before a native `Edit` / `Write` (harness only accepts edits on files previously read via `Read`).
- Latency-native checks where no MCP route exists (rare).

**Default routing for reads and writes:**
- Symbol lookup, navigation, impact analysis, repo-wide search → **jcm**.
- File/symbol read when planning an edit, surgical | regex-based edits inside a known symbol, diagnostics, project memories → **Serena**.
- Create/overwrite files → **Serena** `create_text_text`.
- Listing files, directory scans, glob discovery → **Serena** `list_dir` / `find_file` (preferred) or **jcm** `get_file_tree`.

## 1. Tool routing matrix

Use the FIRST column that fits the goal. Only fall back to the next column if the prior is unavailable.

| User goal | Prefer jcm (repo-wide semantic) | Prefer Serena (LSP / symbolic) | Native fallback |
|---|---|---|---|
| Cold-start orientation | `get_repo_map`, `get_tectonic_map`, `get_repo_health` | `read_memory` (conventions from onboarding) + `onboarding` if never run | — |
| "Find the symbol/function X" | `search_symbols(semantic=true or fusion=true)` | `find_symbol(name_path_pattern, depth>=1)` | — |
| "What does file X look like" | `get_file_outline` → `get_context_bundle` | `get_symbols_overview(relative_path, depth=1..2)` | `Read` (pre-Edit only) |
| "Who calls / uses X" (cross-file) | `find_references`, `get_blast_radius`, `get_call_hierarchy` | `find_referencing_symbols(name_path, relative_path)` | — |
| "Implementations of abstract X" | `find_implementations(repo=..., symbol=...)` | `find_implementations(name_path, relative_path)` (LSP) | — |
| "Where is X defined, from a usage site" | `get_context_bundle(symbol_ids=[id])` | `find_declaration(relative_path, regex)` | — |
| "Scan for pattern / rule / anti-pattern in code" | `search_ast(category=security)` etc. | `search_for_pattern(paths_include_glob, restrict_search_to_code_files=True, multiline=True)` | `Grep` (fallback) |
| "Find string literal / comment / doc" | `search_text(query, context_lines=3)` | `search_for_pattern(restrict_search_to_code_files=False)` | — |
| "List files / tree / glob" | `get_file_tree(path_prefix=...)` | `list_dir(recursive=true)` + `find_file(file_mask)` | `Glob` (fallback) |
| "Read a file I'm going to Edit" | `get_file_content` | `read_file(relative_path, start_line, end_line)` | `Read` (pre-Edit precondition) |
| "Create / overwrite a file" | — | `create_text_file(relative_path, content)` | `Write` (fallback) |
| "Regex-driven substitution in file" | `plan_refactoring(refactor_type=signature)` → edit plan | `replace_content(needle, repl, mode="regex", allow_multiple_occurrences=True)` | `Edit` (preferred once located) |
| Safe symbol delete / rename | `check_safe(mode=edit|delete)` THEN Edit | `safe_delete_symbol` / `rename_symbol` (diagnostic-backed) | `Edit` |
| Diagnose type errors | — | `get_diagnostics_for_file(relative_path, start_line, end_line)` | — |
| Memories / conventions | — | `read_memory`, `write_memory`, `list_memories` | — |
| Diff / PR risk | `get_changed_symbols`, `get_pr_risk_profile` | `execute_shell_command` for git only when needed | — |

## 2. Opening playbook (cold start + per-request)

### 2.1 Cold-start / repo never seen
```
# 1. conventions from onboarding (Serena)
read_memory             # if installed
# 2. confirm index (jcm)
resolve_repo(path=".")
   # if not indexed: index_folder(path=".")
# 3. one-call intent capsule (jcm)
assemble_task_context(repo="animaios/animamunchmcp", task=<user message>, model=<active model>)
   # honor _meta.confidence: high=proceed / medium=narrow / low=report gap, stop
# 4. optionally expand first-contact files
(jcm)  get_file_outline
OR
(Serena) get_symbols_overview(relative_path=<path>, depth=1)
```

### 2.2 Per-request dispatch
- **Explore / read:** jcm `search_symbols(fusion=true)` → `get_context_bundle(symbol_ids=[...])` OR Serena `find_symbol(depth>=1)`.
- **Edit / refactor:** jcm `plan_refactoring` OR `check_safe(mode=edit)` THEN Serena `replace_symbol_body` / `insert_*_symbol` / `rename_symbol` / `safe_delete_symbol` OR `replace_content(regex)` THEN native `Edit`. Use jcm for cross-file blast-radius; prefer Serena for intra-symbol edits.
- **Pattern / security scan:** jcm `search_ast(category=security)` + Serena `search_for_pattern(restrict_search_to_code_files=True)`.
- **Impact / who-uses:** jcm `get_blast_radius(depth=2, include_source=true)` + `find_references`; if more context at each use site, Serena `find_referencing_symbols`.
- **Definition site:** jcm `get_context_bundle(symbol_ids=[id])`; if starting from a usage / regex location, Serena `find_declaration`.
- **Diagnostics / lint:** Serena `get_diagnostics_for_file(relative_path)`.
- **Memory lifecycle:** Serena `read_memory` at session start; Serena `write_memory` for any durable convention discovered.

### 2.3 Post-edit
```
# if Serena language server is active — it auto-updates symbol index.
# if not — push invalidation to jcm:
register_edit(repo="animaios/animamunchmcp", file_paths=[<edited>], reindex=true)

# optional re-check for blast-radius-sensitive edits:
get_blast_radius(symbol=<touched>, depth=2, include_source=true)
get_pr_risk_profile(base_ref="main", head_ref="HEAD")

# sanity-check type-correctness of edited files:
get_diagnostics_for_file(relative_path=<edited>)
```

## 3. jcm MCP cheatsheet (repo-wide semantic)

### Cold-start
- `resolve_repo`, `assemble_task_context` — **ALWAYS the first two calls**. Task capsule = `assemble_task_context(repo="animaios/animamunchmcp", task=...)`.
- `get_repo_map`, `get_tectonic_map`, `get_repo_health`, `get_project_intel` — repo topology, module boundaries, dead-code %, Dockerfiles/CI/deps discovery.

### Read (structured — never full files by default)
- `get_file_outline` → `get_symbol_source` (single ID → flat, array → batch) → `get_context_bundle(symbol_ids=[...], budget_strategy="core_first")`.
- `get_file_content` (last resort, line range), `get_file_tree(path_prefix=)`.

### Search
- `search_symbols` — `mode="context"` (ranked, query-less), `mode="winnow"` (multi-axis), `semantic=true`, `fusion=true` (Weighted Reciprocal Rank), `detail_level="compact"` + `token_budget=3000` for broad discovery.
- `search_text` (full text), `search_ast(category="security"|"all")`, `search_columns` (dbt/SQLMesh).

### Graph / impact
- `find_references` (`mode="refs"|"importers"|"related"`, `quick=true` for dead-code, `cross_repo=true`).
- `get_call_hierarchy(symbol_id=..., direction="both", depth=3)` (+ `chains=true` + `kind="http"|"cli"|` for route discovery).
- `get_blast_radius(symbol=..., include_source=true, call_depth=1, include_decisions=true)`.
- `check_safe(mode="edit"|"delete")`, `plan_refactoring(refactor_type="rename"|"move"|"extract"|"signature")`.

### Bulk & triage
- `find_similar_symbols`, `find_implementations`, `get_class_hierarchy`, `get_symbol_provenance`, `get_symbol_complexity`, `get_dead_code_v2(min_confidence=0.67)`, `find_hot_paths(top_n=20)`.
- `get_changed_symbols(include_blast_radius=true, max_blast_depth=3)`, `get_pr_risk_profile(base_ref="main", head_ref="HEAD")`.

### Index lifecycle
- `index_folder` / `index_file` / `index_repo` / `invalidate_cache`, `embed_repo(force=true, batch_size=50)`, `summarize_repo(force=true)`, `register_edit(file_paths=[...])`.

## 4. Serena MCP cheatsheet (symbolic / LSP)

All paths are **relative to project root** (`/home/vi/animamunchmcp/`).

### Lifecycle
- `activate_project` — call first if project not activated (stdio clients usually do this at startup).
- `get_symbols_overview(relative_path, depth=1)` — first call when learning a new file.
- `initial_instructions` — clients that did not read the 'Serena Instructions Manual' on connect call this IMMEDIATELY.
- `onboarding` — at most once per session; seeds conventions and memories.

### Read
- `read_file(relative_path, start_line=0, end_line=None)` — line-bounded read; prefer over native `Read`.
- `list_dir(relative_path, recursive=true, skip_ignored_files=true)`; `find_file(file_mask, relative_path=".")`.
- `search_for_pattern(substring_pattern, context_lines_before=2, context_lines_after=2, paths_include_glob="", paths_exclude_glob="", restrict_search_to_code_files=false, multiline=true)` — project-wide regex.

### Symbols (read)
- `find_symbol(name_path_pattern, depth=0, include_body=false, include_kinds=[], exclude_kinds=[], substring_matching=false, max_matches=10)`.
  - `name_path` = `Class/method` within a file; absolute = `/Class/method` (full-path match).
- `find_referencing_symbols(name_path, relative_path)` + content-around-reference.
- `find_implementations(name_path, relative_path)` (LSP-based depth).
- `find_declaration(relative_path, regex, containing_symbol_name_path=None)` — given a usage site/regex, resolves the definition.
- `get_diagnostics_for_file(relative_path, start_line=0, end_line=-1, min_severity=4)`.

### Symbols (edit, diagnostic-backed)
- `replace_symbol_body(name_path, relative_path, body)`.
- `insert_after_symbol` / `insert_before_symbol(name_path, relative_path, body)`.
- `rename_symbol(name_path, relative_path, new_name)`.
- `safe_delete_symbol(name_path, relative_path)` — only executes if zero references.
- `replace_content(relative_path, needle, repl, mode="regex"|"literal", allow_multiple_occurrences=False)` — best for regex-in-content edits.

### Content edits
- `insert_at_line`, `delete_lines`, `replace_lines`, `create_text_file(relative_path, content)`.

### Memories
- `read_memory(memory_name)` / `write_memory(memory_name, content)` / `list_memories(topic="")` / `delete_memory(memory_name)` / `rename_memory(old_name, new_name)` / `edit_memory(memory_name, needle, repl, mode)`.

### Operational
- `execute_shell_command(command, cwd=None, capture_stderr=true)` — only short-running shell.
- `open_dashboard`, `get_current_config`, `list_queryable_projects`, `query_project(project_name, tool_name, tool_params_json)`, `remove_project`, `restart_language_server`.

## 5. Hard rules (MUST)

1. The SOP is a **soft default**: prefer jcm or Serena first; native `Read` / `Grep` / `Glob` / `Bash` allowed when required as harness precondition for Edit, or when no comparable MCP route exists.
2. NEVER read full files via native `Read` unless Edit or Write on that file is imminent. Prefer `get_context_bundle`, `get_symbol_source`, or Serena `read_file` (line-bounded).
3. Before ANY edit / delete / rename: run jcm `check_safe(mode=edit|delete)` OR Serena `find_referencing_symbols` / `safe_delete_symbol` — whichever gives the tighter blast radius. Re-run after the change via `get_diagnostics_for_file`.
4. After `search_symbols` returns `_meta.verdict="no_implementation_found"`: DO NOT re-search with different terms and DO NOT assume a related file implements the feature. Report the gap to the user. Only use `related_existing` as "nearby" hint.
5. Honor `_meta.confidence` (0-1): ≥ 0.8 → trust top result; ≤ 0.4 → widen search or report gap, DO NOT proceed as-is.
6. For symbol read operations, prefer Serena's diagnostic-backed tools (`replace_symbol_body`, `safe_delete_symbol`, `rename_symbol`) over blind `Edit` whenever the change maps to a single symbol.
7. Edits with blast radius > same-file → jcm `plan_refactoring` first, then Serena's symbol-level or native Edit per plan.
8. Read Serena `read_memory` once at session start for project conventions.
9. Register edits back to jcm (`register_edit(file_paths=[...])`) when the active runner isn't the Serena language server.

## 6. Anti-patterns (MUST NOT)

- ❌ Full-file Read via native before any `get_file_outline` / `get_symbols_overview`.
- ❌ Manual `Grep` for a query that `search_symbols(semantic=true|fusion=true)` + `search_text` already cover.
- ❌ Blind multi-file `Edit` without `check_safe` / `find_referencing_symbols` / `get_blast_radius` first.
- ❌ String substitution via `Edit` when `replace_content(mode="regex")`, `replace_symbol_body`, `rename_symbol`, or `safe_delete_symbol` applies.
- ❌ Refactoring (rename/move/extract) without `plan_refactoring` when blast radius > same-file.
- ❌ Skipping `get_diagnostics_for_file` after edits that alter signatures or imports.
- ❌ Writing / editing memories directly in `.serena/memories` — only via `write_memory` / `edit_memory`.
- ❌ Searching again after `verdict="no_implementation_found"` — respect the negative evidence.
- ❌ Ignoring `_meta.budget_warning` / `auto_compacted: true` — stop exploring and work with what you have.
- ❌ Manual blast-radius tracing — `get_blast_radius(depth=2, include_source=true)` is instant.
- ❌ Listing files via `Glob` when Serena `list_dir` / `find_file` give the same result faster.

## 7. Token-budget discipline

- `assemble_task_context(token_budget=4000)` — focused tasks.
- `assemble_task_context(token_budget=6000)` + `model=<active>` — large refactor / audit with Sonnet; bump to 10k only on Opus.
- `get_context_bundle(token_budget=6000, budget_strategy="core_first")` — multi-symbol context.
- `search_symbols(token_budget=3000)` + `detail_level="compact"` — broad discovery (≈15 tokens/row).
- Always check `_meta.tokens_used` / `_meta.tokens_remaining`. Stop when `budget_warning` appears.
- Choose `budget_strategy="compact"` on `get_context_bundle` for call-chain mapping (signatures only).

## 8. Model-driven tiering (jcm → assemble_task_context)

Pass `model="<id>"` to `assemble_task_context` so jcm can sub-select the cheapest sufficient tool tier for the active model:

- Claude Opus (`claude-opus-4-7`) → fullest tool tier
- Claude Sonnet (`claude-sonnet-4-6`) → expanded tier
- Claude Haiku (`claude-haiku-4-5`) → minimal tier
- GPT-4o / GPT-5 / o1 / Llama → use printed model id

If `assemble_task_context` is not appropriate for a non-code task, call `announce_model(model="...")` once instead.

---
Repo: `animaios/animamunchmcp`
jcm Symbol ID: `{file_path}::{qualified_name}#{kind}`
Serena project root: `/home/vi/animamunchmcp/`
