"""Source/link and offline-package checks; browser rehearsal is a separate check.

Run: python docs/portfolio/build/test_export.py
"""
import base64
from html.parser import HTMLParser
import importlib.util
import json
from pathlib import Path
import re
import tempfile
import unittest
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


class References(HTMLParser):
    def __init__(self):
        super().__init__()
        self.refs = []
        self.ids = set()

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if 'id' in attrs:
            self.ids.add(attrs['id'])
        for attr in ('src', 'href', 'poster'):
            if attrs.get(attr):
                self.refs.append(attrs[attr])


class PortfolioPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('portfolio_export', ROOT / 'build/export_single_file.py')
        cls.exporter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.exporter)
        cls.pages = {}
        for path in ROOT.rglob('*.html'):
            parser = References()
            parser.feed(path.read_text(encoding='utf-8'))
            cls.pages[path.resolve()] = parser

    def test_local_html_references(self):
        count = 0
        for page, parsed in self.pages.items():
            for ref in parsed.refs:
                url = urlsplit(ref)
                if url.scheme or url.netloc:
                    continue
                target = (page.parent / unquote(url.path)).resolve() if url.path else page
                with self.subTest(page=page.relative_to(ROOT), ref=ref):
                    self.assertTrue(target.is_file(), str(target))
                    self.assertTrue(target.is_relative_to(ROOT),
                                    f'Local link escapes the offline package: {ref}')
                    self.assertNotEqual(target.suffix.lower(), '.md',
                                        f'Markdown is not an embedded reader page: {ref}')
                    if url.fragment and target in self.pages:
                        self.assertIn(unquote(url.fragment), self.pages[target].ids)
                count += 1
        self.assertGreater(count, 100)

    def test_stylesheet_assets(self):
        css_path = ROOT / 'styles/site.css'
        css = css_path.read_text(encoding='utf-8')
        for ref in re.findall(r'url\([\'"]?([^\'"\)]+)', css):
            if not urlsplit(ref).scheme:
                self.assertTrue((css_path.parent / ref).is_file(), ref)

    def test_story_routes(self):
        script = (ROOT / 'scripts/reading.js').read_text(encoding='utf-8')
        story = script.split('const story = [', 1)[1].split('];', 1)[0]
        rows = re.findall(r"\['([^']+)', '([^']+)'", story)
        self.assertGreater(len(rows), 5)
        for page, anchor in rows:
            self.assertIn(anchor, self.pages[(ROOT / page).resolve()].ids)

    def test_dynamic_chart_and_screen_assets(self):
        for seed in (0, 1000):
            for metric in ('mae', 'rmse'):
                for episode in (*range(27, 33), 'pooled'):
                    self.assertTrue((ROOT / f'assets/charts/action-seed{seed}-{metric}-{episode}.svg').is_file())
        for name in ('authoring', 'plan', 'results'):
            self.assertTrue((ROOT / f'assets/screenshots/collection-{name}.png').is_file())

    def test_export_preserves_assets_and_sources(self):
        with tempfile.TemporaryDirectory(prefix='fr5-portfolio-') as directory:
            output = Path(directory) / 'FR5-Portfolio.html'
            size, digest = self.exporter.export(output)
            self.assertLess(size, 50_000_000)
            self.assertEqual(len(digest), 64)
            html = output.read_text(encoding='utf-8')
            payload = re.search(r'id="files">(.*?)</script>', html, re.S).group(1)
            files = json.loads(payload)
            for relative in ('index.html', 'scripts/reading.js', 'styles/site.css', 'sources/motion.html', 'assets/recordings/pick-latest.mp4'):
                self.assertEqual(base64.b64decode(files[relative][1]), (ROOT / relative).read_bytes())
            self.assertFalse(any(name.startswith('build/') for name in files))

    def test_rejects_output_inside_source(self):
        with self.assertRaises(ValueError):
            self.exporter.export(ROOT / 'must-not-be-created.html')
        self.assertFalse((ROOT / 'must-not-be-created.html').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
