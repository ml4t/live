# Release Recovery

Release records are immutable. Do not move a published tag, replace a package file, overwrite a
release asset, or reuse a version after an identity conflict. The machine-readable policy is
[`release-recovery.toml`](https://github.com/ml4t/live/blob/main/release-recovery.toml).

## Partial PyPI Publication

Stop the workflow and compare every published filename and SHA-256 digest with the qualified
inventory. If all published files match, the protected trusted-publishing environment may publish
only the missing qualified file. If any file differs, leave the version unchanged and qualify a new
version.

## PyPI Succeeded And GitHub Release Failed

Verify the PyPI files, immutable tag target, artifact hashes, SBOM, dependency snapshot, and
attestations. If every identity matches, create only the missing GitHub release from the existing
tag and qualified artifacts. Do not publish the package again.

## Tag, Version, Or Release Conflict

Stop before any additional write. Compare the existing record with the qualified commit and
inventory. Do not move a tag or replace a package or release asset. Correct the cause and qualify a
new version.

## Provenance Failure

Stop when trusted-publisher identity, attestation, SBOM, or dependency-snapshot verification fails.
Preserve existing records. If publication already created an immutable record, correct the input
and qualify a new version.
