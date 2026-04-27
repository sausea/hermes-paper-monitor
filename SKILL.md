---
name: hermes-paper-monitor
description: Monitor academic sources for new papers in robotics, navigation, state estimation, GNSS/INS, SLAM, geomagnetics, GPR, and related topics. Use when Codex needs to run a repeatable paper-ingestion workflow across RSS feeds, arXiv API, and journal landing pages; download PDFs when allowed; generate Chinese summaries; fill missing author affiliations; store results in a local SQLite table; and rebuild a local static reading site.
---

# Hermes Paper Monitor

## Overview

Run a three-stage workflow:

1. Discover and normalize candidate papers from the configured sources.
2. Enrich the new-paper queue with Chinese summaries and missing affiliations.
3. Persist the enriched records and rebuild the local static reading site.

Prefer the bundled CLI instead of re-implementing scrapers. The script already handles source configuration, deduplication, SQLite storage, best-effort PDF download, and static-site export.

The bundled default config runs in `abstract-only` mode. That mode monitors new papers, stores title plus abstract, and rebuilds the local reading site without downloading PDFs or waiting for manual enrichment.
If `[llm_translation]` is enabled in the config, `abstract-only` mode can also auto-generate `zh_summary` for English abstracts during discovery.

## Quick Start

Initialize the workspace once:

```bash
python3 scripts/paper_pipeline.py bootstrap --config references/default-config.toml
```

Discover new papers and abstracts:

```bash
python3 scripts/paper_pipeline.py discover --config references/default-config.toml
```

In the default `abstract-only` mode, this updates SQLite and the reading site data without requiring cookies or a follow-up enrichment step.
If you switch `workflow_mode` back to `full`, `discover` also writes a JSONL enrichment queue into `runtime/staging/`.

Backfill missing Chinese summaries for existing records:

```bash
python3 scripts/paper_pipeline.py translate-missing \
  --config references/default-config.toml \
  --limit 50
```

If you switch to full enrichment mode, import the completed queue with:

```bash
python3 scripts/paper_pipeline.py finalize \
  --config references/default-config.toml \
  --input runtime/staging/enrichment-YYYYMMDDTHHMMSSZ.jsonl
```

Rebuild the reading site:

```bash
python3 scripts/paper_pipeline.py build-site --config references/default-config.toml
```

Run a local background web service and open the site in a browser:

```bash
python3 scripts/paper_pipeline.py serve \
  --config references/default-config.toml
```

This serves the site on `http://127.0.0.1:8765/` by default and can periodically refresh the paper catalog in the background.

## Workflow

### 1. Bootstrap

Run `bootstrap` before the first scheduled job. It creates the SQLite database and the output directories declared in the config.

### 2. Discover

Run `discover` whenever the user asks for new-paper monitoring or a daily refresh.

What it does:

- Read `references/default-config.toml`.
- Fetch source entries from:
  - RSS feeds
  - arXiv API
  - HTML issue/list pages with article-link heuristics
- Normalize metadata with citation meta tags when article pages expose them.
- Filter by configured keywords and arXiv categories.
- Deduplicate by DOI, arXiv id, landing URL, or normalized title hash.
- In `abstract-only` mode, stop after title, abstract, DOI, authors, and other metadata are stored.
- In `full` mode, download PDFs when a direct PDF URL is available and access is permitted.
- Upsert records into SQLite.
- In `full` mode, emit an enrichment queue for records that still need `zh_summary` or affiliation cleanup.

Use `--skip-downloads` if a source is rate-limited or access-controlled and you only need metadata on that run.

### 3. Enrich

Skip this section in the default `abstract-only` mode.

Open the enrichment queue and write back JSONL lines that include:

- `dedupe_key` or `paper_id`
- `zh_summary`
- `affiliations`
- optional `notes`

Read [references/enrichment-schema.md](/Users/usr/dls/skill/hermes-paper-monitor/references/enrichment-schema.md) for the exact fields.

Quality bar for enrichment:

- Write the Chinese summary from the abstract or full text when available.
- Keep the summary factual and compact.
- Fill affiliations from `citation_author_institution` if present.
- If affiliations are missing, infer them from the paper landing page or a reliable publisher metadata page.
- If a gated site blocks the full article or PDF, keep the record and note the restriction instead of inventing data.

### 4. Finalize

Skip this step in the default `abstract-only` mode.

Run `finalize` after enrichment. It updates the SQLite rows and marks the records `ready` when both the Chinese summary and affiliations are present.

### 5. Build The Site

Run `build-site` after any ingest or finalize step. The script writes:

- `site/index.html`
- `site/styles.css`
- `site/app.js`
- `site/catalog.js`
- `site/catalog.json`
- `site/papers.csv`

The site is intentionally self-contained, so opening `site/index.html` directly from the filesystem still works.

## Source Strategy

Use these operating rules:

- Prefer RSS and arXiv API when available. They are the most stable.
- Use HTML list-page scraping for journal homepages and recent-issue pages.
- Treat abstract and metadata extraction as cookie-free by default whenever the landing page is publicly reachable.
- Expect IEEE Xplore and some ScienceDirect pages to require institution cookies or headers only for restricted PDF download.
- Treat PDF download as best-effort. Never fail the entire run because one publisher returns `403` or `401`.
- If no cookie file is configured, keep ingesting title, abstract, DOI, and affiliations; mark the PDF as skipped instead of failing the run.
- Keep duplicate source entries out of the config. The provided list included `punumber=6979` twice; the bundled default config deduplicates it.

## Automation Pattern

When the user asks for a daily run in the default `abstract-only` mode, use this sequence:

1. `bootstrap` once if the workspace is empty.
2. `discover`
3. `build-site`

If the user later wants PDF download, Chinese summaries, or affiliation completion, switch `workflow_mode` to `full` and restore:

1. `bootstrap`
2. `discover`
3. Read the emitted enrichment queue.
4. Write Chinese summaries and cleaned affiliations into a second JSONL file.
5. `finalize`
6. `build-site`

If the environment supports scheduling, prefer a daily local-time run that opens an inbox item with the new-paper count and any restricted-download notes.

## Resources

- [references/default-config.toml](/Users/usr/dls/skill/hermes-paper-monitor/references/default-config.toml): source list, keyword list, and output paths
- [references/enrichment-schema.md](/Users/usr/dls/skill/hermes-paper-monitor/references/enrichment-schema.md): JSONL schema for the enrichment queue
- [references/test-config.toml](/Users/usr/dls/skill/hermes-paper-monitor/references/test-config.toml): offline smoke-test configuration
- [scripts/paper_pipeline.py](/Users/usr/dls/skill/hermes-paper-monitor/scripts/paper_pipeline.py): main CLI
- [assets/site/index.html](/Users/usr/dls/skill/hermes-paper-monitor/assets/site/index.html): static-site shell
