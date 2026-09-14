# Fresh-project evidence review

Initial assessment reserves space for the root overview (or shallowest available README) and, when present, shared component setup documentation. Remaining excerpt slots contain manifests. The four-excerpt and 36,000-character budgets remain in force.

A host-generated declaredDefault traces bounded literal root npm dev aliases, falling back to start when dev is absent. It never runs scripts. For a direct Vite command with an in-repository literal --config path, the config directory provides a target component hint. Unsupported wrappers, cycles, and ambiguous syntax are not guessed.

The policy prefers this default plus required runtime dependencies; shared workspace libraries alone do not justify launching another app. If the first response is needs_input, the host performs a follow-up regardless of whether the model requested read_more. It supplies up to three extra excerpts, prioritizing the default's config, unread documentation, and remaining manifests. The original response and reason for the review are included. No previous run history is required.

When a concrete default target was detected, an initial plan containing multiple services with no dependencies also receives review. If that unconnected multi-service plan persists, selection still requires input. This check is conservative: it does not prove all dependency graphs correct or guarantee identical model outputs. Real ambiguity, conflicts in documentation, unavailable configuration, and evidence-budget failures remain possible.

The existing one-follow-up limit is preserved. Trace cards distinguish a host-initiated evidence review from model-requested file reading. Planning does not execute project commands.

Initial launch plans are also validated before acceptance. A validation failure uses the same single follow-up allowance, with the rejected assessment and exact validation error supplied for correction. A second invalid response stops; this does not expand the planning budget or execute rejected commands.

Validation feedback now collects independent metadata problems in one pass: command/plan validation, all bounded evidence citations, missing/unknown/duplicate component IDs, and missing exclusion reasons. The follow-up receives validationErrors plus a readable summary, so a malformed citation cannot hide an omitted workspace root. Every discovered ID, including ".", must be accounted for once. The two-call assessment budget is unchanged.

If a response has valid citations plus malformed references, accepted plans retain only the valid citations as evidence and show the others separately as unvalidated notes. A plan with no valid citations still fails. This avoids letting an extra narrative reference block an otherwise validated plan, without fabricating line numbers or suppressing the original response.
