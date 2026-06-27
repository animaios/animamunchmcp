## jcodemunch

Repo: `animaios/animamunchmcp` (indexed). Symbol ID: `{file_path}::{qualified_name}#{kind}`

### Core lookup
- `resolve_repo(path=".")` — confirm the project is indexed. If not: `index_folder(path=".")`. **Always call this first in a new session.**
- `assemble_task_context(repo="animaios/animamunchmcp", task="...")` — opening move; auto-classifies intent (explore/debug/refactor/extend/audit/review), surfaces symbols + ranked context
- `get_file_outline` → `get_symbol_source` / `get_context_bundle(symbol_ids=[...])` — targeted retrieval, never full files
- `search_symbols(repo="animaios/animamunchmcp", query="...")` — find by name, signature, summary
  - `mode="context"` — query-less ranked context assembly
  - `mode="winnow"` — multi-axis constraint filter (kind, language, complexity, churn, etc.)
  - `semantic=true` — embedding-based search (requires embed provider)
  - `fusion=true` — multi-signal fusion (Weighted Reciprocal Rank) across lexical/structural/similarity/identity channels; best for vague queries
  - `detail_level="compact"` — returns id/name/kind/file/line only (~15 tokens/row); use with `token_budget` for broad discovery
  - `decorator="X"` — find symbols with a specific decorator (e.g. `@property`, `@route`); combine with set-difference to find symbols *lacking* a decorator
- `search_text(repo="animaios/animamunchmcp", query="...")` — full-text search across file contents (string literals, comments, configs)
- `search_ast(repo="animaios/animamunchmcp", pattern="..." | category="...")` — structural anti-pattern scan (empty_catch, god_function, hardcoded_secret, etc.)
- `search_columns(repo="animaios/animamunchmcp", query="...")` — search column metadata across indexed models (dbt/SQLMesh)
- `find_references(repo="animaios/animamunchmcp", identifier="..." | identifiers=[...])` — find all files that import or reference an identifier via the import graph
  - `mode="refs"` (default) — original find_references
  - `mode="importers"` — find files importing a given file
  - `mode="related"` — find symbols related to a given symbol (use `symbol_id`)
  - `quick=true` — lightweight `is_referenced` envelope (import_count + content_count) for fast dead-code detection
  - `cross_repo=true` — also search other indexed repos for cross-repo importers

### Code Exploration Policy

Always use jCodemunch-MCP tools for code navigation. Never fall back to Read, Grep, Glob, or Bash for code exploration.
**Exception:** Use `Read` when you need to edit a file — the agent harness requires a `Read` before `Edit`/`Write` will succeed. Use jCodemunch tools to *find and understand* code, then `Read` only the specific file you're about to modify.

**Start any session:**
1. `resolve_repo { "path": "." }` — confirm the project is indexed. If not: `index_folder { "path": "." }`

**Reading code:**
- before opening any file → `get_file_outline` first
- one or more symbols → `get_symbol_source` (single ID → flat object; array → batch)
- symbol + its imports → `get_context_bundle`
- specific line range only → `get_file_content` (last resort)

**Repo structure:**
- `get_repo_map(mode="outline")` → dirs, languages, symbol counts
- `get_file_tree` → file layout, filter with `path_prefix`

### Impact & safety
- `get_blast_radius(symbol="...", include_source=true)` — check impact before changes; add `call_depth=1` to also find symbols that *call* this symbol; `include_decisions=true` surfaces git commit intent (revert/perf/refactor/bugfix); `source_budget` caps snippet tokens
- `find_references(mode="refs"|"importers"|"related", quick=true)` / `get_call_hierarchy(symbol_id="...", direction="both", depth=3)` — trace who uses a symbol; `get_call_hierarchy` also supports `chains=true` to discover HTTP routes / CLI commands / events involving this symbol, filtered by `kind` (http/cli/event/task/main/test), with `max_depth` BFS limit
- `check_safe(repo="animaios/animamunchmcp", symbol="...", mode="edit"|"delete")` — composite preflight: can this symbol be safely edited/deleted?
- `plan_refactoring(repo="animaios/animamunchmcp", symbol="...", refactor_type="rename"|"move"|"extract"|"signature")` — generate multi-file edit plan before refactoring
- `get_changed_symbols(repo="animaios/animamunchmcp")` — map git diff to affected symbols
- `get_pr_risk_profile(repo="animaios/animamunchmcp")` — unified risk assessment for a PR/branch

### Repository intelligence
- `get_repo_health(repo="animaios/animamunchmcp")` — one-call triage (dead code %, complexity, hotspots, cycle count)
- `get_repo_map(repo="animaios/animamunchmcp")` — signature-level overview ranked by PageRank (cold-start orientation); `mode="outline"` for lightweight dirs/languages/symbol counts only
- `get_tectonic_map(repo="animaios/animamunchmcp")` — logical module topology (hidden boundaries, misplaced files, drifters)
- `find_hot_paths(repo="animaios/animamunchmcp")` — top-N symbols by runtime hit count (requires ingested traces)
- `get_dead_code_v2(repo="animaios/animamunchmcp", min_confidence=0.67)` — multi-signal dead code detection
- `find_similar_symbols(repo="animaios/animamunchmcp")` — cluster similar functions/methods (consolidation candidates)
- `get_symbol_provenance(repo="animaios/animamunchmcp", symbol="...")` — git authorship lineage & evolution narrative
- `get_symbol_complexity(repo="animaios/animamunchmcp", symbol_id="...")` — cyclomatic complexity, nesting, params
- `get_class_hierarchy(repo="animaios/animamunchmcp", class_name="...")` — inheritance ancestors + descendants
- `find_implementations(repo="animaios/animamunchmcp", symbol="...")` — find concrete impls of an interface/abstract
- `get_project_intel(repo="animaios/animamunchmcp")` — auto-discover Dockerfiles, CI configs, deps, APIs
- `list_workspaces(repo="animaios/animamunchmcp")` — enumerate monorepo workspace members

### Runtime & indexing
- `import_runtime_signal(repo="animaios/animamunchmcp", path="...", source="otel"|"sql_log"|"stack_log")` — ingest runtime traces
- `embed_repo(repo="animaios/animamunchmcp")` — precompute symbol embeddings for semantic search; `force=true` to recompute all; `batch_size` controls symbols per embedding batch (default 50)
- `summarize_repo(repo="animaios/animamunchmcp", force=true)` — re-run AI summarization pipeline
- `index_file(path="...")` — surgical single-file reindex after edits
- `index_folder(path="...")` / `index_repo(url="...")` — full index/reindex
- `register_edit(repo="animaios/animamunchmcp", file_paths=[...], reindex=true)` — invalidate caches after file edits

### Session-Aware Routing

**Opening move for any task:**
1. `assemble_task_context(repo="animaios/animamunchmcp", task="your task description")` — returns a prioritized, token-budgeted context capsule. Auto-classifies intent (explore / debug / refactor / extend / audit / review) and runs the appropriate tool chain in one call.
2. Obey the confidence level:
   - `high` → go directly to recommended symbols, max 2 supplementary reads
   - `medium` → explore recommended files, max 5 supplementary reads
   - `low` → the feature likely doesn't exist. Report the gap to the user. Do NOT search further hoping to find it.

**Interpreting search results:**
- If `search_symbols` returns `negative_evidence` with `verdict: "no_implementation_found"`:
  - Do NOT re-search with different terms hoping to find it
  - Do NOT assume a related file (e.g. auth middleware) implements the missing feature (e.g. CSRF)
  - DO report: "No existing implementation found for X. This would need to be created."
  - DO check `related_existing` files — they show what's nearby, not what exists
- If `verdict: "low_confidence_matches"`: examine the matches critically before assuming they implement the feature

**After editing files:**
- If PostToolUse hooks are installed (Claude Code only), edited files are auto-reindexed
- Otherwise, call `register_edit` with edited file paths to invalidate caches and keep the index fresh
- For bulk edits (5+ files), always use `register_edit` with all paths to batch-invalidate

**Token efficiency:**
- If `_meta` contains `budget_warning`: stop exploring and work with what you have
- If `auto_compacted: true` appears: results were automatically compressed due to turn budget
- Use `get_session_context` to check what you've already read — avoid re-reading the same files

**Reading the response envelope (v1.74.0+):**
- `_meta.confidence` (0–1) — calibrated retrieval-quality score on `search_symbols` / `assemble_task_context`. ≥ 0.8 → trust the top result; ≤ 0.4 → widen the search or report a gap
- `_meta.freshness` — `{fresh, edited_uncommitted, stale_index}` counts plus `repo_is_stale` flag. Per-result `_freshness` field on each symbol entry
- If `repo_is_stale=true`, suggest `index_folder` before claiming current behaviour
- For latency / cache health: `analyze_perf` (in-memory by default; `window=1h|24h|7d|all` reads `~/.code-index/telemetry.db` when `perf_telemetry_enabled` is on)
- After a representative workload on a new repo, run `tune_weights` to learn per-repo retrieval weights from the ranking ledger

## Model-Driven Tool Tiering

Pass `model="<your-model-id>"` to `assemble_task_context` to let the server optimize which sub-tools it runs for your capability level.

Replace `<your-model-id>` with your active model:
- Claude Opus variants → `claude-opus-4-7` (or any `claude-opus-*`)
- Claude Sonnet variants → `claude-sonnet-4-6`
- Claude Haiku variants → `claude-haiku-4-5`
- GPT-4o / GPT-5 / o1 / Llama → use the model id as printed by your runner

If `assemble_task_context` is not appropriate for a non-code task, call `announce_model(model="...")` once instead.

### Power User Guide

#### Golden Rules
1. **Always start with `assemble_task_context`** — it auto-classifies intent and returns ranked symbols + context in one call. Never manually hunt for entry points.
2. **Batch everything** — use `symbol_ids[]` in `get_context_bundle`, `get_symbol_source`, `search_symbols` instead of serial calls. Token budget is your friend.
3. **Verify with `verify=true` / `verify_against="git_sha"`** — catches index drift vs. working tree.
4. **Use `mode` switches** on `search_symbols`: `context` for query-less ranked context, `winnow` for multi-axis filters, `semantic=true` for embedding search.
5. **Prefer `get_context_bundle` over raw file reads** — deduplicates imports, respects token budget, returns ready-to-use context.

#### Common Workflows

##### 1. Cold-start orientation (new repo / unfamiliar area)
```
get_repo_map(repo="animaios/animamunchmcp", group_by="flat", top_n=30)     # Top symbols by PageRank
get_tectonic_map(repo="animaios/animamunchmcp")                               # Logical module boundaries
get_repo_health(repo="animaios/animamunchmcp", detailed=true)                 # Dead code %, complexity, cycles
```

##### 2. Feature exploration — "How does X work?"
```
assemble_task_context(repo="animaios/animamunchmcp", task="How does X work?")
# → returns ranked symbols + context
get_context_bundle(symbol_ids=[...], budget_strategy="core_first")
```

##### 3. Refactoring safety (rename/move/extract)
```
check_safe(repo="animaios/animamunchmcp", symbol="SymbolName", mode="edit")
plan_refactoring(repo="animaios/animamunchmcp", symbol="SymbolName", refactor_type="rename", new_name="newName")
get_blast_radius(symbol="SymbolName", depth=2, include_source=true)
```

##### 4. Dead code cleanup
```
get_dead_code_v2(repo="animaios/animamunchmcp", min_confidence=0.67, file_pattern="src/**")
find_similar_symbols(repo="animaios/animamunchmcp", threshold=0.85, include_kinds=["function", "method"])
```

##### 5. Performance hotspot triage
```
find_hot_paths(repo="animaios/animamunchmcp", top_n=20)
get_repo_health(repo="animaios/animamunchmcp", detailed=true, top_n=30)
get_symbol_complexity(repo="animaios/animamunchmcp", symbol_id="...")
```

##### 6. PR / change risk assessment
```
get_changed_symbols(repo="animaios/animamunchmcp", include_blast_radius=true, max_blast_depth=3)
get_pr_risk_profile(repo="animaios/animamunchmcp", base_ref="main", head_ref="HEAD")
```

##### 7. Understanding unfamiliar code before modifying
```
get_symbol_provenance(repo="animaios/animamunchmcp", symbol="SymbolName", max_commits=30)
get_call_hierarchy(symbol_id="...", direction="both", depth=3, include_impact=true)
find_implementations(repo="animaios/animamunchmcp", symbol="InterfaceName", include_subclasses=true)
```

##### 8. Finding config / string literals / comments (not symbols)
```
search_text(repo="animaios/animamunchmcp", query="MAX_RETRIES", context_lines=3)
search_ast(repo="animaios/animamunchmcp", category="security")              # hardcoded_secret, eval_exec
search_ast(repo="animaios/animamunchmcp", pattern="string:/password/i")      # custom pattern
```

#### Parameter Cheatsheet

| Tool | Key params | Key param combos | When to use |
|---|---|---|---|
| `resolve_repo` | `path` | `path="."` | **First call in a new session** — confirm repo is indexed |
| `assemble_task_context` | `task`, `token_budget` (8k default), `model` | `model="claude-sonnet-4-6"` | **First call for any task** — returns intent, symbols, context |
| `search_symbols` | `mode`, `semantic`, `fusion`, `detail_level`, `token_budget` | `fusion=true` for vague queries; `detail_level="compact"` + `token_budget=3000` for broad discovery | Symbol discovery; `mode=context` = ranked context w/o query |
| `get_context_bundle` | `symbol_ids[]`, `budget_strategy`, `token_budget` | `budget_strategy="compact"` for signatures only; `budget_strategy="core_first"` keeps primary symbol | Multi-symbol context in one call |
| `find_references` | `identifier`/`identifiers`, `mode`, `quick`, `cross_repo` | `quick=true` for dead-code check; `mode="importers"` for file-level deps; `cross_repo=true` across repos | Find importers / references / related symbols |
| `get_blast_radius` | `depth`, `include_source`, `include_depth_scores`, `call_depth`, `include_decisions`, `source_budget` | `call_depth=1` + `include_decisions=true` for full impact + git intent | Pre-edit impact; `include_depth_scores` = per-hop risk |
| `get_call_hierarchy` | `symbol_id`, `direction`, `depth`, `chains`, `kind`, `max_depth`, `include_impact`, `include_decisions` | `chains=true` for HTTP routes / CLI commands; `include_decisions=true` on `include_impact=true` | Trace callers/callees; `include_impact=true` for deletion safety |
| `check_safe` | `mode` (edit/delete), `include_runtime` | Use before every edit or delete | Preflight — returns verdict + top-5 blockers |
| `plan_refactoring` | `refactor_type`, `new_name`/`new_file`/`new_signature` | `refactor_type="signature"` + `new_signature="..."` | Returns `{old_text, new_text}` blocks ready for Edit tool |
| `get_repo_health` | `detailed`, `rules` (layer defs) | `detailed=true` for cycles, coupling, hotspots | One-call triage |
| `get_repo_map` | `group_by`, `top_n`, `mode`, `scope` | `mode="outline"` for lightweight overview; `group_by="flat"` for ranked symbol list | Cold-start orientation |
| `get_tectonic_map` | `days`, `min_plate_size` | — | Module topology; finds drifters, nexus plates (coupled ≥4) |
| `find_similar_symbols` | `threshold`, `semantic_weight`, `include_tests` | `semantic_weight=0.6` default; `threshold=0.85` for strict match | Consolidation candidates |
| `get_symbol_provenance` | `max_commits` | `max_commits=30` for deep history | Authorship lineage + evolution narrative |
| `search_ast` | `category`, `pattern`, `language` | `category="security"` for hardcoded_secret, eval_exec; `category="all"` runs everything | Anti-pattern sweep |
| `get_changed_symbols` | `since_sha`, `until_sha`, `include_blast_radius` | `include_blast_radius=true` + `max_blast_depth=3` | Maps git diff → symbols + downstream impact |
| `get_pr_risk_profile` | `base_ref`, `head_ref`, `days` | — | Composite risk score (blast + complexity + churn + tests + volume) |
| `embed_repo` | `force`, `batch_size` | `force=true` to recompute all | Precompute embeddings; run once then `semantic=true` works instantly |

#### Anti-patterns to Avoid
- ❌ Reading full files with `read_file` — use `get_context_bundle` or `get_symbol_source`
- ❌ Calling `search_symbols` repeatedly — batch with `symbol_ids[]` in `get_context_bundle`
- ❌ Skipping `check_safe` before edits/deletes — 5s call prevents hours of revert
- ❌ Not verifying with `verify=true` — index can drift from working tree
- ❌ Using `grep` for symbol lookup — `search_symbols` understands signatures, imports, types
- ❌ Manual blast radius tracing — `get_blast_radius(depth=2, include_source=true)` is instant
- ❌ Ignoring `_meta.confidence` < 0.4 — low confidence means widen the search or report a gap, not proceed as-is

#### Pro Tips
- **`fusion=true` on `search_symbols`** — uses Weighted Reciprocal Rank across lexical/structural/similarity/identity channels; best for vague queries
- **`budget_strategy="compact"`** on `get_context_bundle` — returns signatures only (min tokens), great for call-chain mapping
- **`include_decisions=true`** on `get_blast_radius` / `get_call_hierarchy(include_impact=true)` — surfaces git commit intent (revert/perf/refactor/bugfix) from history
- **`embed_repo(repo="animaios/animamunchmcp")` once** — then `semantic=true` on `search_symbols` works instantly for semantic queries
- **`index_file` after every edit** — keeps index fresh for subsequent tool calls in same session
- **`cross_repo=true`** on `get_blast_radius` / `find_references` — finds consumers in other indexed repos

#### Token Budget Discipline
- `assemble_task_context(token_budget=4000)` for focused tasks
- `get_context_bundle(token_budget=6000, budget_strategy="core_first")` for multi-symbol context
- `search_symbols(token_budget=3000)` with `detail_level="compact"` for broad discovery (15 tokens/row)
- Always check `_meta.tokens_used` / `_meta.tokens_remaining` in responses
