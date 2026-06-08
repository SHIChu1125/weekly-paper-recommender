import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import paper_weekly


class PaperWeeklyTests(unittest.TestCase):
    def config(self):
        return paper_weekly.Config(
            name="Test",
            timezone="Asia/Shanghai",
            weekly_limit=2,
            lookback_days=7,
            keywords=["Thermal comfort", "Cooling garment", "Heat Stress"],
            broad_keywords=["Heat Stress"],
            exclude_keywords=["battery thermal management"],
            sources={"semantic_scholar": True, "arxiv": True},
            site={"title": "Test Site", "description": "Test"},
        )

    def test_score_filters_and_matches_keywords(self):
        cfg = self.config()
        paper = paper_weekly.Paper(
            paper_id="p1",
            source="Semantic Scholar",
            title="Cooling garment improves outdoor thermal comfort",
            abstract="A field study evaluates heat stress and cooling performance.",
            authors=["A"],
            published="2026-06-01",
            url="https://example.com",
        )

        scored = paper_weekly.score_paper(paper, cfg, dt.date(2026, 6, 8))

        self.assertIsNotNone(scored)
        self.assertIn("Thermal comfort", scored.matched_keywords)
        self.assertIn("Cooling garment", scored.matched_keywords)
        self.assertGreater(scored.score, 10)

    def test_exclude_keywords_remove_paper(self):
        cfg = self.config()
        paper = paper_weekly.Paper(
            paper_id="p1",
            source="Semantic Scholar",
            title="Battery thermal management cooling strategy",
            abstract="This is not about personal cooling.",
            authors=[],
            published="2026-06-01",
            url="",
        )

        self.assertIsNone(paper_weekly.score_paper(paper, cfg, dt.date(2026, 6, 8)))

    def test_broad_keyword_alone_does_not_admit_paper(self):
        cfg = self.config()
        paper = paper_weekly.Paper(
            paper_id="p1",
            source="Semantic Scholar",
            title="Plant responses to heat stress",
            abstract="A molecular biology study of heat stress in crops.",
            authors=[],
            published="2026-06-01",
            url="",
        )

        self.assertIsNone(paper_weekly.score_paper(paper, cfg, dt.date(2026, 6, 8)))

    def test_dedupe_keeps_highest_score(self):
        cfg = self.config()
        low = paper_weekly.Paper(
            paper_id="low",
            source="arXiv",
            title="Thermal comfort garment",
            abstract="Cooling garment.",
            authors=[],
            published="2026-06-01",
            url="",
            doi="10.1/demo",
        )
        high = paper_weekly.Paper(
            paper_id="high",
            source="Semantic Scholar",
            title="Cooling garment for thermal comfort and heat stress",
            abstract="Thermal comfort heat stress cooling garment cooling performance.",
            authors=[],
            published="2026-06-07",
            url="",
            doi="10.1/demo",
            citation_count=5,
        )

        ranked = paper_weekly.dedupe_and_rank([low, high], cfg, dt.date(2026, 6, 8))

        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0].paper_id, "high")

    def test_translation_falls_back_without_api_key(self):
        paper = paper_weekly.Paper(
            paper_id="p1",
            source="Mock",
            title="Thermal comfort",
            abstract="Abstract",
            authors=[],
            published="2026-06-01",
            url="",
        )

        paper_weekly.translate_papers([paper])

        self.assertEqual(paper.title_zh, "待翻译")
        self.assertEqual(paper.translation_status, "missing_api_key")

    def test_build_site_with_mock_data(self):
        mock = [
            {
                "id": "p1",
                "source": "Mock",
                "title": "Cooling garment improves thermal comfort",
                "abstract": "This paper studies heat stress and cooling performance outdoors.",
                "authors": ["A", "B"],
                "published": "2026-06-04",
                "url": "https://example.com/p1",
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            path = tmp_path / "mock.json"
            docs_dir = tmp_path / "docs"
            old_docs_dir = paper_weekly.DOCS_DIR
            old_data_dir = paper_weekly.DATA_DIR
            paper_weekly.DOCS_DIR = docs_dir
            paper_weekly.DATA_DIR = docs_dir / "data"
            try:
                path.write_text(json.dumps(mock), encoding="utf-8")
                rc = paper_weekly.build_site(
                    ROOT / "config" / "topics.yml",
                    "2026-06-08",
                    path,
                    skip_translate=True,
                )
            finally:
                paper_weekly.DOCS_DIR = old_docs_dir
                paper_weekly.DATA_DIR = old_data_dir
            self.assertEqual(rc, 0)
            self.assertTrue((docs_dir / "index.html").exists())
            self.assertTrue((docs_dir / "feed.xml").exists())


if __name__ == "__main__":
    unittest.main()
