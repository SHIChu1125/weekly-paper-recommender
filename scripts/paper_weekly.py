#!/usr/bin/env python3
"""Build a weekly bilingual paper recommendation site."""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import html
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "topics.yml"
DOCS_DIR = ROOT / "docs"
DATA_DIR = DOCS_DIR / "data"
USER_AGENT = "weekly-paper-recommender/1.0 (mailto:example@example.com)"


@dataclass
class Config:
    name: str
    timezone: str
    weekly_limit: int
    lookback_days: int
    keywords: list[str]
    broad_keywords: list[str]
    exclude_keywords: list[str]
    sources: dict[str, bool]
    site: dict[str, str]


@dataclass
class Paper:
    paper_id: str
    source: str
    title: str
    abstract: str
    authors: list[str]
    published: str
    url: str
    doi: str = ""
    arxiv_id: str = ""
    venue: str = ""
    citation_count: int = 0
    matched_keywords: list[str] = field(default_factory=list)
    score: float = 0.0
    title_zh: str = ""
    abstract_zh: str = ""
    translation_status: str = "pending"

    def key(self) -> str:
        if self.doi:
            return "doi:" + self.doi.lower()
        if self.arxiv_id:
            return "arxiv:" + self.arxiv_id.lower()
        normalized = re.sub(r"\W+", " ", self.title.lower()).strip()
        return "title:" + normalized


def parse_scalar(value: str) -> Any:
    value = value.strip()
    if value in {"[]", ""}:
        return [] if value == "[]" else ""
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    return value


def load_simple_yaml(path: Path) -> dict[str, Any]:
    """Parse the small YAML subset used by config/topics.yml."""

    root: dict[str, Any] = {}
    lines = path.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        if not raw.strip() or raw.lstrip().startswith("#"):
            i += 1
            continue
        if raw.startswith(" "):
            raise ValueError(f"Unexpected indentation at line {i + 1}: {raw}")
        key, sep, value = raw.partition(":")
        if not sep:
            raise ValueError(f"Invalid YAML line {i + 1}: {raw}")
        key = key.strip()
        value = value.strip()
        if value:
            root[key] = parse_scalar(value)
            i += 1
            continue

        block: list[str] = []
        i += 1
        while i < len(lines) and (not lines[i].strip() or lines[i].startswith("  ")):
            if lines[i].strip():
                block.append(lines[i][2:])
            i += 1
        if not block:
            root[key] = {}
        elif all(item.startswith("- ") for item in block):
            root[key] = [parse_scalar(item[2:]) for item in block]
        else:
            child: dict[str, Any] = {}
            for item in block:
                child_key, sep, child_value = item.partition(":")
                if not sep:
                    raise ValueError(f"Invalid nested YAML under {key}: {item}")
                child[child_key.strip()] = parse_scalar(child_value)
            root[key] = child
    return root


def load_config(path: Path = CONFIG_PATH) -> Config:
    data = load_simple_yaml(path)
    return Config(
        name=str(data["name"]),
        timezone=str(data.get("timezone", "Asia/Shanghai")),
        weekly_limit=int(data.get("weekly_limit", 10)),
        lookback_days=int(data.get("lookback_days", 7)),
        keywords=[str(item) for item in data.get("keywords", [])],
        broad_keywords=[str(item) for item in data.get("broad_keywords", [])],
        exclude_keywords=[str(item) for item in data.get("exclude_keywords", [])],
        sources={k: bool(v) for k, v in data.get("sources", {}).items()},
        site={k: str(v) for k, v in data.get("site", {}).items()},
    )


def request_json(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    req_headers = {"User-Agent": USER_AGENT}
    req_headers.update(headers or {})
    req = urllib.request.Request(url, headers=req_headers)
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def request_json_with_retry(
    url: str, headers: dict[str, str] | None = None, attempts: int = 3
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return request_json(url, headers)
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code != 429 or attempt == attempts - 1:
                raise
            retry_after = exc.headers.get("Retry-After")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else 2.0 * (attempt + 1)
            time.sleep(delay)
    assert last_error is not None
    raise last_error


def request_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as response:
        return response.read().decode("utf-8")


def fetch_semantic_scholar(config: Config, start: dt.date, end: dt.date) -> list[Paper]:
    if not config.sources.get("semantic_scholar", False):
        return []
    headers = {}
    api_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
    if api_key:
        headers["x-api-key"] = api_key

    papers: list[Paper] = []
    fields = ",".join(
        [
            "paperId",
            "title",
            "abstract",
            "authors",
            "year",
            "publicationDate",
            "url",
            "externalIds",
            "citationCount",
            "venue",
        ]
    )
    date_filter = f"{start.isoformat()}:{end.isoformat()}"
    for keyword in config.keywords:
        query = urllib.parse.urlencode(
            {
                "query": keyword,
                "limit": "50",
                "fields": fields,
                "publicationDateOrYear": date_filter,
            }
        )
        url = f"https://api.semanticscholar.org/graph/v1/paper/search?{query}"
        try:
            payload = request_json_with_retry(url, headers)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            print(f"warn: Semantic Scholar query failed for {keyword!r}: {exc}", file=sys.stderr)
            continue
        for item in payload.get("data", []):
            title = clean_text(item.get("title") or "")
            abstract = clean_text(item.get("abstract") or "")
            if not title or not abstract:
                continue
            external = item.get("externalIds") or {}
            authors = [a.get("name", "") for a in item.get("authors", []) if a.get("name")]
            papers.append(
                Paper(
                    paper_id=str(item.get("paperId") or external.get("DOI") or title),
                    source="Semantic Scholar",
                    title=title,
                    abstract=abstract,
                    authors=authors,
                    published=str(item.get("publicationDate") or item.get("year") or ""),
                    url=str(item.get("url") or ""),
                    doi=str(external.get("DOI") or ""),
                    arxiv_id=str(external.get("ArXiv") or ""),
                    venue=str(item.get("venue") or ""),
                    citation_count=int(item.get("citationCount") or 0),
                )
            )
        time.sleep(1.0 if api_key else 2.0)
    return papers


def fetch_arxiv(config: Config, start: dt.date, end: dt.date) -> list[Paper]:
    if not config.sources.get("arxiv", False):
        return []

    papers: list[Paper] = []
    start_stamp = start.strftime("%Y%m%d0000")
    end_stamp = (end + dt.timedelta(days=1)).strftime("%Y%m%d0000")
    ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    for keyword in config.keywords:
        phrase = urllib.parse.quote(f'"{keyword}"')
        search = f'all:{phrase}+AND+submittedDate:[{start_stamp}+TO+{end_stamp}]'
        params = f"search_query={search}&start=0&max_results=50&sortBy=submittedDate&sortOrder=descending"
        url = f"https://export.arxiv.org/api/query?{params}"
        try:
            xml_text = request_text(url)
            root = ET.fromstring(xml_text)
        except (urllib.error.URLError, TimeoutError, ET.ParseError) as exc:
            print(f"warn: arXiv query failed for {keyword!r}: {exc}", file=sys.stderr)
            continue
        for entry in root.findall("atom:entry", ns):
            title = clean_text(entry.findtext("atom:title", default="", namespaces=ns))
            abstract = clean_text(entry.findtext("atom:summary", default="", namespaces=ns))
            arxiv_id = entry.findtext("atom:id", default="", namespaces=ns).rsplit("/", 1)[-1]
            published = entry.findtext("atom:published", default="", namespaces=ns)[:10]
            authors = [
                clean_text(author.findtext("atom:name", default="", namespaces=ns))
                for author in entry.findall("atom:author", ns)
            ]
            url_link = entry.findtext("atom:id", default="", namespaces=ns)
            doi = entry.findtext("arxiv:doi", default="", namespaces=ns)
            papers.append(
                Paper(
                    paper_id=arxiv_id or title,
                    source="arXiv",
                    title=title,
                    abstract=abstract,
                    authors=[a for a in authors if a],
                    published=published,
                    url=url_link,
                    doi=doi,
                    arxiv_id=arxiv_id,
                )
            )
        time.sleep(3.1)
    return papers


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def parse_date(value: str) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError:
        match = re.search(r"\d{4}", value)
        if match:
            return dt.date(int(match.group(0)), 1, 1)
    return None


def keyword_match(text: str, keyword: str) -> bool:
    text_l = text.lower()
    keyword_l = keyword.lower()
    if keyword_l in text_l:
        return True
    terms = [term for term in re.split(r"\W+", keyword_l) if len(term) > 2]
    return bool(terms) and all(term in text_l for term in terms)


def score_paper(paper: Paper, config: Config, run_date: dt.date) -> Paper | None:
    searchable = f"{paper.title}\n{paper.abstract}"
    lowered = searchable.lower()
    if any(ex.lower() in lowered for ex in config.exclude_keywords):
        return None

    matched = [kw for kw in config.keywords if keyword_match(searchable, kw)]
    if not matched:
        return None
    broad_keywords = {kw.lower() for kw in config.broad_keywords}
    specific_matches = [kw for kw in matched if kw.lower() not in broad_keywords]
    if not specific_matches:
        return None

    score = 0.0
    title_l = paper.title.lower()
    abstract_l = paper.abstract.lower()
    for kw in matched:
        kw_l = kw.lower()
        title_weight = 2.0 if kw_l in broad_keywords else 6.0
        abstract_weight = 1.0 if kw_l in broad_keywords else 3.0
        fuzzy_title_weight = 1.5 if kw_l in broad_keywords else 4.0
        fuzzy_abstract_weight = 0.5 if kw_l in broad_keywords else 1.5
        if kw_l in title_l:
            score += title_weight
        elif keyword_match(paper.title, kw):
            score += fuzzy_title_weight
        if kw_l in abstract_l:
            score += abstract_weight
        elif keyword_match(paper.abstract, kw):
            score += fuzzy_abstract_weight

    published = parse_date(paper.published)
    if published:
        age_days = max((run_date - published).days, 0)
        score += max(0.0, 3.0 - age_days * 0.25)

    if paper.citation_count:
        score += min(math.log1p(paper.citation_count), 5.0) * 0.5

    if paper.source == "Semantic Scholar":
        score += 0.5

    paper.matched_keywords = matched
    paper.score = round(score, 3)
    return paper


def dedupe_and_rank(papers: list[Paper], config: Config, run_date: dt.date) -> list[Paper]:
    best: dict[str, Paper] = {}
    for paper in papers:
        scored = score_paper(paper, config, run_date)
        if not scored:
            continue
        key = scored.key()
        existing = best.get(key)
        if not existing or scored.score > existing.score:
            best[key] = scored
    ranked = sorted(
        best.values(),
        key=lambda item: (item.score, parse_date(item.published) or dt.date.min),
        reverse=True,
    )
    return ranked[: config.weekly_limit]


def translate_papers(papers: list[Paper]) -> None:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        for paper in papers:
            paper.title_zh = "待翻译"
            paper.abstract_zh = "待翻译"
            paper.translation_status = "missing_api_key"
        return

    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    items = [
        {"id": paper.paper_id, "title": paper.title, "abstract": paper.abstract}
        for paper in papers
    ]
    prompt = (
        "Translate the following paper titles and abstracts into Simplified Chinese. "
        "Be faithful to the source text. Do not summarize, add interpretation, or omit technical terms. "
        "Return JSON only as {\"translations\":[{\"id\":\"...\",\"title_zh\":\"...\",\"abstract_zh\":\"...\"}]}.\n\n"
        + json.dumps(items, ensure_ascii=False)
    )
    payload = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": "You are a precise academic translator."},
            {"role": "user", "content": prompt},
        ],
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as response:
            data = json.loads(response.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
        translations = json.loads(content).get("translations", [])
        by_id = {str(item.get("id")): item for item in translations}
    except (urllib.error.URLError, TimeoutError, KeyError, IndexError, json.JSONDecodeError) as exc:
        print(f"warn: translation failed: {exc}", file=sys.stderr)
        for paper in papers:
            paper.title_zh = "待翻译"
            paper.abstract_zh = "待翻译"
            paper.translation_status = "translation_failed"
        return

    for paper in papers:
        translated = by_id.get(paper.paper_id)
        if not translated:
            paper.title_zh = "待翻译"
            paper.abstract_zh = "待翻译"
            paper.translation_status = "translation_missing"
        else:
            paper.title_zh = clean_text(str(translated.get("title_zh") or "待翻译"))
            paper.abstract_zh = clean_text(str(translated.get("abstract_zh") or "待翻译"))
            paper.translation_status = "translated"


def papers_to_dicts(papers: list[Paper]) -> list[dict[str, Any]]:
    return [
        {
            "id": paper.paper_id,
            "source": paper.source,
            "title": paper.title,
            "title_zh": paper.title_zh,
            "abstract": paper.abstract,
            "abstract_zh": paper.abstract_zh,
            "authors": paper.authors,
            "published": paper.published,
            "url": paper.url,
            "doi": paper.doi,
            "arxiv_id": paper.arxiv_id,
            "venue": paper.venue,
            "citation_count": paper.citation_count,
            "matched_keywords": paper.matched_keywords,
            "score": paper.score,
            "translation_status": paper.translation_status,
        }
        for paper in papers
    ]


def write_json(run_date: dt.date, start: dt.date, end: dt.date, config: Config, papers: list[Paper]) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "topic": config.name,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "week_start": start.isoformat(),
        "week_end": end.isoformat(),
        "keywords": config.keywords,
        "papers": papers_to_dicts(papers),
    }
    path = DATA_DIR / f"{run_date.isoformat()}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def paper_card(paper: Paper, index: int) -> str:
    authors = ", ".join(paper.authors[:6])
    if len(paper.authors) > 6:
        authors += " et al."
    badges = "".join(f"<span>{html.escape(kw)}</span>" for kw in paper.matched_keywords)
    doi = f'<a href="https://doi.org/{html.escape(paper.doi)}">DOI</a>' if paper.doi else ""
    arxiv = f'<a href="https://arxiv.org/abs/{html.escape(paper.arxiv_id)}">arXiv</a>' if paper.arxiv_id else ""
    links = " ".join(
        item for item in [f'<a href="{html.escape(paper.url)}">Paper</a>' if paper.url else "", doi, arxiv] if item
    )
    return f"""
      <article class="paper">
        <div class="rank">{index:02d}</div>
        <div class="paper-body">
          <h2>{html.escape(paper.title)}</h2>
          <h3>{html.escape(paper.title_zh)}</h3>
          <p class="meta">{html.escape(authors)} · {html.escape(paper.published)} · {html.escape(paper.source)} · Score {paper.score:.2f}</p>
          <div class="links">{links}</div>
          <p class="abstract en">{html.escape(paper.abstract)}</p>
          <p class="abstract zh">{html.escape(paper.abstract_zh)}</p>
          <div class="badges">{badges}</div>
        </div>
      </article>
    """


def html_page(title: str, body: str, config: Config) -> str:
    site_title = config.site.get("title", config.name)
    description = config.site.get("description", "")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)} · {html.escape(site_title)}</title>
  <meta name="description" content="{html.escape(description)}">
  <link rel="alternate" type="application/rss+xml" title="{html.escape(site_title)} RSS" href="feed.xml">
  <style>
    :root {{
      --bg: #f7f8f3;
      --ink: #17201b;
      --muted: #5c665f;
      --line: #d9ded5;
      --paper: #ffffff;
      --accent: #006f79;
      --accent-2: #b24a36;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      background: var(--bg);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.55;
    }}
    header, main, footer {{ width: min(1120px, calc(100% - 32px)); margin: 0 auto; }}
    header {{ padding: 40px 0 24px; border-bottom: 1px solid var(--line); }}
    .kicker {{ color: var(--accent); font-weight: 700; text-transform: uppercase; font-size: 13px; letter-spacing: .08em; }}
    h1 {{ margin: 8px 0 10px; font-size: clamp(30px, 5vw, 56px); line-height: 1.05; letter-spacing: 0; }}
    .summary {{ max-width: 820px; color: var(--muted); font-size: 17px; }}
    nav {{ display: flex; gap: 12px; flex-wrap: wrap; margin-top: 20px; }}
    nav a, .links a {{
      color: var(--accent);
      text-decoration: none;
      font-weight: 700;
      border-bottom: 1px solid color-mix(in srgb, var(--accent), transparent 55%);
    }}
    .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin: 24px 0; }}
    .stat {{ border: 1px solid var(--line); background: var(--paper); border-radius: 8px; padding: 14px 16px; }}
    .stat strong {{ display: block; font-size: 26px; }}
    .paper {{
      display: grid;
      grid-template-columns: 64px 1fr;
      gap: 18px;
      background: var(--paper);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 20px;
      margin: 16px 0;
    }}
    .rank {{
      width: 48px;
      height: 48px;
      display: grid;
      place-items: center;
      border-radius: 50%;
      color: white;
      background: var(--accent);
      font-weight: 800;
    }}
    .paper h2 {{ margin: 0 0 6px; font-size: 22px; line-height: 1.25; letter-spacing: 0; }}
    .paper h3 {{ margin: 0 0 10px; font-size: 18px; line-height: 1.35; color: var(--accent-2); letter-spacing: 0; }}
    .meta {{ color: var(--muted); margin: 0 0 8px; font-size: 14px; }}
    .links {{ display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 12px; }}
    .abstract {{ margin: 10px 0; }}
    .zh {{ color: #24352f; }}
    .badges {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }}
    .badges span {{
      border: 1px solid var(--line);
      background: #eef3eb;
      border-radius: 999px;
      padding: 4px 9px;
      font-size: 12px;
      color: var(--muted);
    }}
    .archive-list {{ background: var(--paper); border: 1px solid var(--line); border-radius: 8px; padding: 16px 20px; }}
    .archive-list li {{ margin: 8px 0; }}
    footer {{ color: var(--muted); padding: 28px 0 40px; }}
    @media (max-width: 640px) {{
      .paper {{ grid-template-columns: 1fr; padding: 16px; }}
      .rank {{ width: 40px; height: 40px; }}
      h1 {{ font-size: 34px; }}
    }}
  </style>
</head>
<body>
{body}
</body>
</html>
"""


def write_index(config: Config, run_date: dt.date, start: dt.date, end: dt.date, papers: list[Paper]) -> None:
    cards = "\n".join(paper_card(paper, idx) for idx, paper in enumerate(papers, start=1))
    keyword_list = ", ".join(config.keywords)
    body = f"""
<header>
  <div class="kicker">Weekly Papers · {html.escape(start.isoformat())} to {html.escape(end.isoformat())}</div>
  <h1>{html.escape(config.site.get("title", config.name))}</h1>
  <p class="summary">{html.escape(config.site.get("description", ""))}</p>
  <nav><a href="archive.html">Archive</a><a href="feed.xml">RSS</a></nav>
</header>
<main>
  <section class="stats" aria-label="weekly stats">
    <div class="stat"><strong>{len(papers)}</strong>recommended papers</div>
    <div class="stat"><strong>{config.lookback_days}</strong>day window</div>
    <div class="stat"><strong>{len(config.keywords)}</strong>tracking keywords</div>
  </section>
  <p class="summary"><strong>Keywords:</strong> {html.escape(keyword_list)}</p>
  {cards if cards else '<p>No matching papers were found for this window.</p>'}
</main>
<footer>Generated on {html.escape(run_date.isoformat())}. Chinese titles and abstracts are faithful translations of source metadata.</footer>
"""
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "index.html").write_text(html_page("Latest", body, config), encoding="utf-8")


def archive_entries() -> list[tuple[str, int]]:
    entries: list[tuple[str, int]] = []
    if not DATA_DIR.exists():
        return entries
    for path in sorted(DATA_DIR.glob("*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            entries.append((path.stem, len(data.get("papers", []))))
        except json.JSONDecodeError:
            continue
    return entries


def write_archive(config: Config) -> None:
    items = "\n".join(
        f'<li><a href="data/{html.escape(date)}.json">{html.escape(date)}</a> · {count} papers</li>'
        for date, count in archive_entries()
    )
    body = f"""
<header>
  <div class="kicker">Archive</div>
  <h1>{html.escape(config.site.get("title", config.name))}</h1>
  <nav><a href="index.html">Latest</a><a href="feed.xml">RSS</a></nav>
</header>
<main>
  <ol class="archive-list">{items if items else '<li>No archived issues yet.</li>'}</ol>
</main>
<footer>Each archive JSON file contains the full bilingual weekly paper metadata.</footer>
"""
    (DOCS_DIR / "archive.html").write_text(html_page("Archive", body, config), encoding="utf-8")


def write_feed(config: Config, run_date: dt.date, papers: list[Paper]) -> None:
    base_url = config.site.get("base_url", "").rstrip("/")
    site_title = config.site.get("title", config.name)
    channel_link = f"{base_url}/" if base_url else "index.html"
    items = []
    for paper in papers:
        link = paper.url or channel_link
        description = f"{paper.title_zh}\n\n{paper.abstract_zh}\n\n{paper.abstract}"
        items.append(
            f"""
    <item>
      <title>{html.escape(paper.title)}</title>
      <link>{html.escape(link)}</link>
      <guid isPermaLink="false">{html.escape(paper.key())}</guid>
      <pubDate>{email.utils.format_datetime(dt.datetime.combine(parse_date(paper.published) or run_date, dt.time(), tzinfo=dt.timezone.utc))}</pubDate>
      <description>{html.escape(description)}</description>
    </item>"""
        )
    feed = f"""<?xml version="1.0" encoding="UTF-8" ?>
<rss version="2.0">
  <channel>
    <title>{html.escape(site_title)}</title>
    <link>{html.escape(channel_link)}</link>
    <description>{html.escape(config.site.get("description", ""))}</description>
    <lastBuildDate>{email.utils.format_datetime(dt.datetime.now(dt.timezone.utc))}</lastBuildDate>
    {''.join(items)}
  </channel>
</rss>
"""
    (DOCS_DIR / "feed.xml").write_text(feed, encoding="utf-8")


def determine_window(config: Config, run_date_arg: str | None) -> tuple[dt.date, dt.date, dt.date]:
    tz = ZoneInfo(config.timezone)
    if run_date_arg:
        run_date = dt.date.fromisoformat(run_date_arg)
    else:
        run_date = dt.datetime.now(tz).date()
    start = run_date - dt.timedelta(days=config.lookback_days)
    end = run_date - dt.timedelta(days=1)
    return run_date, start, end


def load_mock_papers(path: Path) -> list[Paper]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    papers = []
    for item in payload:
        papers.append(
            Paper(
                paper_id=str(item.get("id") or item.get("title")),
                source=str(item.get("source", "Mock")),
                title=str(item.get("title", "")),
                abstract=str(item.get("abstract", "")),
                authors=[str(a) for a in item.get("authors", [])],
                published=str(item.get("published", "")),
                url=str(item.get("url", "")),
                doi=str(item.get("doi", "")),
                arxiv_id=str(item.get("arxiv_id", "")),
                venue=str(item.get("venue", "")),
                citation_count=int(item.get("citation_count", 0)),
            )
        )
    return papers


def build_site(config_path: Path, run_date_arg: str | None, mock_path: Path | None, skip_translate: bool) -> int:
    config = load_config(config_path)
    run_date, start, end = determine_window(config, run_date_arg)
    if mock_path:
        fetched = load_mock_papers(mock_path)
    else:
        fetched = []
        fetched.extend(fetch_semantic_scholar(config, start, end))
        fetched.extend(fetch_arxiv(config, start, end))

    ranked = dedupe_and_rank(fetched, config, run_date)
    if skip_translate:
        for paper in ranked:
            paper.title_zh = "待翻译"
            paper.abstract_zh = "待翻译"
            paper.translation_status = "skipped"
    else:
        translate_papers(ranked)

    write_json(run_date, start, end, config, ranked)
    write_index(config, run_date, start, end, ranked)
    write_archive(config)
    write_feed(config, run_date, ranked)
    print(f"Generated {len(ranked)} papers for {run_date.isoformat()} in {DOCS_DIR}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--run-date", help="Local run date in YYYY-MM-DD format.")
    parser.add_argument("--mock-papers", type=Path, help="Use a local JSON paper list instead of remote APIs.")
    parser.add_argument("--skip-translate", action="store_true", help="Generate pages without calling OpenAI.")
    args = parser.parse_args(argv)
    return build_site(args.config, args.run_date, args.mock_papers, args.skip_translate)


if __name__ == "__main__":
    raise SystemExit(main())
