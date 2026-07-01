# Branch Protection

## branch_protection signal: PASS

> Created at 2026-07-01 15:02 UTC+3 · GH account `vi70x4` (admin=true)

## Current state

- `gh api repos/vi70x4/animamunchmcp --jq '.permissions.admin'` → `true`
- `gh api repos/vi70x4/animamunchmcp/rulesets` → two rulesets present, both `enforcement: "active"`
- Legacy `branches/main/protection` → was 404 "Branch not protected" (now superseded by rulesets; rulesets take precedence)

### Rulesets applied

| ID    | Name | Target | Enforced refs |
|-------|------|--------|---------------|
| [18377394](https://github.com/animaios/animamunchmcp/rules/18377394) | main — PR reviews + required checks | branch | `refs/heads/main` |
| [18377424](https://github.com/animaios/animamunchmcp/rules/18377424) | default branch — enforce for admins + PR reviews | branch | `~DEFAULT_BRANCH` |

### PR protections configured (both rulesets)

- Require 1 approving review (`required_approving_review_count: 1`)
- Dismiss stale reviews on push (`dismiss_stale_reviews_on_push: true`)
- Require code owner review (`require_code_owner_review: true`)
- Require approval from most recent push (`require_last_push_approval: true`)
- Block force pushes (`non_fast_forward`)
- Required status checks (strict — branches must be up-to-date):
  1. `test` (from `test.yml`)
  2. `lint` (from `test.yml`)
  3. `large-file-check` (from `quality-gates.yml`)
  4. `unused-deps-check` (from `quality-gates.yml`)
  5. `check-agents-md` (from `agents_md_check.yml`)

### CODEOWNERS-based required review

- `.github/CODEOWNERS` exists with:
  - `@jcodemunch/maintainers` owns `/README.md`, `/pyproject.toml`, `/LICENSE`, `/CHANGELOG.md`, `/CONTRIBUTING.md`, `/CODEOWNERS`, `/.github/`
  - `@jcodemunch/developers` owns `/src/`, `/tests/`, `/docs/`, `/AGENTS.md`, `/ARCHITECTURE.md`, `/SPEC.md`
- `require_code_owner_review: true` ensures CODEOWNERS files are reviewed by the declared owners.

### Admin enforcement

- The `main` ruleset does **not** bypass `enforce_admins` (maintainers are subject to PR reviews via CODEOWNERS).
- The `default branch` ruleset mirrors the same protections and uses `~DEFAULT_BRANCH` so the protection survives a default-branch rename.

## Jsonnet-friendly template

`docs/ruleset-payload.jsonnet`:

```jsonnet
// Template — create a branch ruleset requiring PR reviews + status checks.
// Usage: update statuses, then:
//   gh api -X POST "repos/{owner}/{repo}/rulesets" --input ruleset-payload.jsonnet
{
  name: "main — PR reviews + required checks",
  target: "branch",
  enforcement: "active",
  bypass_actors: [],
  conditions: {
    ref_name: {
      include: ["refs/heads/main"],
      exclude: [],
    },
  },
  rules: [
    {
      type: "pull_request",
      parameters: {
        required_approving_review_count: 1,
        dismiss_stale_reviews_on_push: true,
        require_code_owner_review: true,
        require_last_push_approval: true,
        required_review_thread_resolution: false,
      },
    },
    {
      type: "required_status_checks",
      parameters: {
        strict_required_status_checks_policy: true,
        required_status_checks: [
          { context: "test" },
          { context: "lint" },
          { context: "large-file-check" },
          { context: "unused-deps-check" },
          { context: "check-agents-md" },
        ],
      },
    },
    { type: "non_fast_forward" },
  ],
}
```

To create a second ruleset protecting the default branch with enforce_admins=true,
change:
- `name`: "default branch — enforce for admins + PR reviews"
- `conditions.ref_name.include`: `["~DEFAULT_BRANCH"]`
- Keep the same rules list (admins are PR-gated because `bypass_actors: []`)

## Signal summary

- `branch_protection`: **PASS**
  - admin API access verified
  - rulesets applied and `active`
  - PR reviews required with CODEOWNERS
  - status checks enforced
  - non-fast-forward (force-pushes) blocked
