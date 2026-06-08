# Weekly Paper Recommender

Static bilingual weekly paper recommendations for personal thermal management research.

## What it does

- Searches recent papers from Semantic Scholar and arXiv.
- Scores papers against `config/topics.yml`.
- Keeps the top 10 papers for the previous 7-day window.
- Translates titles and abstracts into Simplified Chinese with OpenAI when `OPENAI_API_KEY` is configured.
- Generates `docs/index.html`, `docs/archive.html`, `docs/feed.xml`, and weekly JSON data files for GitHub Pages.

## Configure

Edit `config/topics.yml` to change keywords, broad keywords, exclude keywords, source switches, site copy, and weekly limit. Broad keywords such as `Heat Stress` can improve ranking but cannot admit a paper by themselves.

For GitHub Actions, configure these repository secrets:

- `OPENAI_API_KEY`: optional but needed for Chinese translation.
- `SEMANTIC_SCHOLAR_API_KEY`: optional; improves rate limits.

Optionally set repository variable `OPENAI_MODEL`; otherwise the workflow uses `gpt-4o-mini`.

## Run locally

```powershell
C:\Users\55438\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe scripts\paper_weekly.py --run-date 2026-06-08 --skip-translate
```

Use mock data for a network-free smoke test:

```powershell
C:\Users\55438\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe scripts\paper_weekly.py --run-date 2026-06-08 --mock-papers tests\fixtures\mock_papers.json --skip-translate
```

## Deploy

Enable GitHub Pages with GitHub Actions as the source. The included workflow runs every Sunday at 22:00 UTC, which is Monday 06:00 in Asia/Shanghai.
