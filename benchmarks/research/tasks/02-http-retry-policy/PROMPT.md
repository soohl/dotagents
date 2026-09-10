# Synthesize an HTTP retry and redirect policy

Research the authoritative RFC Editor text for RFC 6585, RFC 9110, and RFC
7538. Use separate searches for the 429 rule, Retry-After semantics, and the
308 rule. Reconcile the normative language rather than relying on summaries.

Create `policy.json` with exactly this shape and the exact enum spellings shown:

```json
{
  "status_429": {
    "name": "...",
    "meaning": "rate limiting",
    "retry_after_requirement": "MAY"
  },
  "retry_after": {
    "forms": ["HTTP-date", "delay-seconds"],
    "delay_seconds_constraint": "non-negative decimal integer",
    "with_503_indicates": "expected service unavailability duration"
  },
  "status_308": {
    "name": "...",
    "permanent": true,
    "cacheable_by_default": true,
    "post_to_get_allowed": false
  },
  "sources": ["https://...", "https://...", "https://..."]
}
```

Cite an HTTPS RFC Editor URL for each RFC. Do not create any other
deliverable.
