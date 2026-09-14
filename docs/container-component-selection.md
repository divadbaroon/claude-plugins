# Assess relevance before enforcing container requirements

Discovery no longer rejects an entire repository merely because a Compose service
exists. The Run Order Agent receives all inventory IDs, container flags, bounded
Compose excerpts and nearby README evidence. It must select the intended app and
its required dependencies, with reasons for exclusions. It may request more
Compose or README evidence before asking the user.

The host rejects execution only when a selected component requires containers.
Such selections retain their evidence and show a needs-input message naming the
container services; no native launch plan is saved. Container execution itself is
not implemented. Directory names alone never establish an exclusion.

Discovery admits up to 64 components, while upfront Railpack analysis is capped at
six native candidates (root and scripted candidates first). All discovered IDs
still reach assessment. Excerpts are shortened within the existing prompt budget,
with truncation marked. Existing limits of four launch services and eight
preparation commands remain. Large inventories still have bounded limits.

Verification covers a fixture excluded after an actual assessment-provider call
(using a deterministic test provider), a selected required container that prevents
execution, and a large inventory with bounded Railpack work. The local OpenScience
inventory contained 17 components; test-provider assessment and evidence follow-up
both fit the prompt budget. This verifies the assessment path, not app startup.
