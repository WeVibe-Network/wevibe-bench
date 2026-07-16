# Recall Corpus and Gold Fixture Schema

This directory contains a fixed public corpus and a gold query fixture for recall-readiness benchmarking.

## 1) Corpus file (`corpus/*.json`)

The corpus file is a single JSON object:

```json
{
  "topic": "string",
  "description": "string",
  "memories": [
    {
      "id": "stable_slug_string",
      "text": "memory text",
      "keywords": ["string", "..."],
      "stack_hint": "comma,separated,hint"
    }
  ]
}
```

- `topic`: corpus domain label.
- `description`: human-readable description of the corpus.
- `memories`: fixed list of memory entries.
  - `id`: stable source slug (canonical memory identifier).
  - `text`: memory content.
  - `keywords`: keyword list used for retrieval/scoring features.
  - `stack_hint`: short stack/context hint string.

## 2) Gold file (`gold/*.gold.jsonl`)

The gold fixture is JSONL: one JSON object per line.

```json
{
  "case_id": "string",
  "category": "single_hit | near_tie | cross_stack_negative | thin_prompt | no_match",
  "query": "string",
  "expected_slugs": ["stable_slug_string", "..."],
  "expect_injection": true,
  "session": {
    "language": "string",
    "stack": ["string", "..."],
    "frameworks": ["string", "..."],
    "deps": ["string", "..."],
    "errorStrings": ["string", "..."],
    "directory": "string",
    "projectName": "string"
  },
  "notes": "optional string"
}
```

### Category meaning and expected result

- `single_hit`: one intended in-domain match. `expected_slugs` is non-empty and `expect_injection` is `true`.
- `near_tie`: two adjacency-designed contenders expected together. `expected_slugs` is non-empty and `expect_injection` is `true`.
- `cross_stack_negative`: out-of-domain/cross-stack query. `expected_slugs` is `[]` and `expect_injection` is `false`.
- `thin_prompt`: sparse query/minimal session that should still match. `expected_slugs` is non-empty and `expect_injection` is `true`.
- `no_match`: in-domain-adjacent topic intentionally uncovered by the corpus. `expected_slugs` is `[]` and `expect_injection` is `false`.

## 3) Identifier rule (slugs vs CIDs)

Gold labels must reference **stable source slugs** (`expected_slugs` values equal corpus `id` values), **never ciphertext-derived CIDs**.

- CIDs are resolved by the resolver for each seed run.
- Resolved CIDs are run-scoped artifacts.
- Resolved CIDs are not canonical labels and are never committed as replacements for slug-based gold labels.

## 4) Isolation requirement

Gold labels and any resolved-CID mapping must be physically isolated from the benchmark worker process. A benchmarked agent must not be able to read gold answers or resolved labels during execution.

## 5) Near-tie interpretation

Near-tie cases are adjacency-designed. Their contested top-score gap (`<0.20`) is confirmed at scoring time, not asserted by this schema file.
