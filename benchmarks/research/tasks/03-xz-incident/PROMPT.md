# Triangulate the XZ Utils supply-chain incident

Build a compact incident dossier for the XZ Utils backdoor. Research and
reconcile at least these source classes with separate searches:

- Andres Freund's original oss-security disclosure;
- the XZ project maintainer's incident page;
- Red Hat's Fedora/RHEL advisory;
- one additional query that cross-checks the CVE or affected releases.

Create `dossier.json` with exactly this shape:

```json
{
  "incident": {
    "cve": "...",
    "disclosure_date": "YYYY-MM-DD",
    "discoverer": "...",
    "affected_upstream_versions": ["...", "..."]
  },
  "construction": {
    "trigger_only_in_release_tarballs": true,
    "second_stage_artifacts_in_git": true
  },
  "targeting": {
    "architecture": "...",
    "os": "...",
    "libc": "...",
    "package_build_contexts": ["...", "..."]
  },
  "impact_path": ["...", "...", "..."],
  "red_hat": {
    "rhel_affected": false,
    "affected_fedora_streams": ["...", "..."],
    "recommended_safe_series": "..."
  },
  "sources": ["https://...", "https://...", "https://..."]
}
```

Use the component dependency order from the exposed service toward the
backdoored library for `impact_path`. Use `Debian` and `RPM` as the package
build context labels. Cite HTTPS URLs from all three required source classes.
After writing the dossier, read it back and verify its structure and facts.
Do not create any other deliverable.
