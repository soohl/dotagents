# Verify Python packaging standards

Research the current official Python packaging specifications. Do not answer
from memory. Use separate web searches for PEP 621 and project-name
normalization, and prefer `peps.python.org` and `packaging.python.org`.

Create `findings.json` with exactly this shape:

```json
{
  "pep_621": {
    "title": "...",
    "status": "...",
    "created": "YYYY-MM-DD"
  },
  "normalization": {
    "input": "Friendly_Bard.baz",
    "output": "...",
    "replacement_pattern": "[-_.]+",
    "replacement": "-"
  },
  "sources": ["https://...", "https://..."]
}
```

Record PEP 621's exact title, status, and creation date. Normalize the supplied
name using the specification's lowercase-and-replace rule. Cite at least two
HTTPS official-source URLs, including the PEP itself and the PyPA
normalization specification.

Do not create any other deliverable.
