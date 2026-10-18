# Contributing to ML4T Live

External submissions follow the
[shared ML4T contribution policy](https://github.com/ml4t/ecosystem/blob/main/CONTRIBUTING.md).
Search existing issues first. Describe one problem or coherent change per issue and submit one
focused PR linked to its owning issue. Automated dependency updates and administrative metadata
changes retain the shared exception.

Maintainer-directed maintenance may batch fixes and related documentation in one PR linked to all
affected issues. It does not require a separate PR for each issue or follow-up edit. Preserve each
fix's regression evidence and contributor attribution, and run applicable checks on the combined
result before merging.

For defects, provide a minimal reproduction through the public API or user workflow, package and
environment details, and expected behavior. Record a regression failing on the relevant base and
passing on the fix, with exact revisions, commands, and results. Correct verified numerical defects;
agree on intentional changes to definitions, annualization, signs, or defaults in the owning issue
and describe their compatibility impact. Documentation changes use relevant documentation checks.

Disclose whether AI assisted with code or prose and identify its contribution. Review and test your
submission yourself, and be prepared to explain its implementation, evidence, and review responses.

Start integration work in shadow or paper mode. Preserve explicit live-routing selection,
provider qualification, recovery behavior, and credential isolation.
Use the repository's [quality commands](AGENTS.md#quality-commands) and report their outcomes.
Report suspected vulnerabilities through
[private security reporting](https://github.com/ml4t/live/security/advisories/new).
