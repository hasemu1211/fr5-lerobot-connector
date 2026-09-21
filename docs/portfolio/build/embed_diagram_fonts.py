"""Embed only the glyphs used by authored SVG diagrams for portable typography.

Editing utility: requires fonttools and brotli. The HTML exporter needs neither.
Usage: python docs/portfolio/build/embed_diagram_fonts.py diagrams/example.svg
Paths are relative to docs/portfolio. Re-run after changing diagram text.
"""
import base64
import io
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

from fontTools import subset
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parents[1]
BLOCK = re.compile(r'<style id="embedded-diagram-fonts">.*?</style>\s*', re.S)


def embed(relative):
    path = (ROOT / relative).resolve()
    if not path.is_relative_to(ROOT) or path.suffix != '.svg':
        raise ValueError('Expected an SVG path inside docs/portfolio')
    source = BLOCK.sub('', path.read_text(encoding='utf-8'))
    tree = ET.fromstring(source)
    characters = ''.join(''.join(node.itertext()) for node in tree.iter('{http://www.w3.org/2000/svg}text'))
    rules = []
    for weight, filename in [(400, 'noto-kr-medium.woff2'), (700, 'noto-kr-bold.woff2')]:
        font = TTFont(ROOT / 'assets/fonts' / filename)
        options = subset.Options()
        selection = subset.Subsetter(options=options)
        selection.populate(text=characters)
        selection.subset(font)
        output = io.BytesIO()
        font.flavor = 'woff2'
        font.save(output)
        font.close()
        data = base64.b64encode(output.getvalue()).decode('ascii')
        rules.append("@font-face{font-family:PortfolioDiagram;font-weight:" + str(weight)
                     + ";src:url(data:font/woff2;base64," + data + ") format('woff2')}")
    rules.append('text{font-family:PortfolioDiagram,sans-serif!important}')
    style = '<style id="embedded-diagram-fonts">' + ''.join(rules) + '</style>\n'
    # Preserve the editable geometry and text, including its formatting.
    # XML declarations and DOCTYPEs may precede the SVG root.
    opening = re.search(r'<svg\b[^>]*>', source)
    if opening is None:
        raise ValueError('Expected an SVG root element')
    position = opening.end()
    result = source[:position] + '\n' + style + source[position:]
    path.write_text(result, encoding='utf-8', newline='\n')
    print(f'{relative}: {len(set(characters))} glyphs, {len(result.encode("utf-8")):,} bytes')


if __name__ == '__main__':
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    for argument in sys.argv[1:]:
        embed(argument)
