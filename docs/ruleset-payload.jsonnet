// Template — create a branch ruleset requiring PR reviews + status checks.
// Usage: update statuses, then:
//   gh api -X POST "repos/{owner}/{repo}/rulesets" --input ruleset-payload.jsonnet
// (GitHub doesn't natively parse Jsonnet; use e.g. `jsonnetfmt` / `drone jsonnet` to
// expand to JSON first if desired — the body below is valid JSON so it passes `gh` directly.)
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
