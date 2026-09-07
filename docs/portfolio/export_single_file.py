#!/usr/bin/env python3
"""Export the existing FR5 reader as one HTML; no server or third-party runtime.

Usage: python3 docs/portfolio/export_single_file.py /path/to/FR5-Portfolio.html
The source reader stays editable. Binary assets are embedded once, byte for byte.
"""
from pathlib import Path
import base64
import hashlib
import json
import mimetypes
import re
import sys

SHELL = r'''<!doctype html><html lang="ko"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FR5 포트폴리오</title><style>
html,body{margin:0;width:100%;height:100%;background:white;color:#17252d}
iframe{display:block;border:0;width:100%;height:100%}#loading{padding:40px;font:18px sans-serif}
</style><div id="loading" role="status">FR5 포트폴리오를 여는 중입니다.</div>
<noscript>이 파일의 페이지 탐색과 그래프에는 JavaScript가 필요합니다.</noscript>
<iframe id="reader" title="FR5 포트폴리오" hidden></iframe>
<script type="application/json" id="files">__FILES__</script>
<script>
'use strict';
const packed = JSON.parse(document.getElementById('files').textContent);
const assets = new Map(), text = new Map(), virtualBase = 'https://fr5.invalid/';
const decoder = new TextDecoder();
for (const [path, [mime, encoded]] of Object.entries(packed)) {
  const bytes = Uint8Array.from(atob(encoded), c => c.charCodeAt(0));
  assets.set(path, URL.createObjectURL(new Blob([bytes], {type:mime})));
  if (/\.(html|css|js)$/.test(path)) text.set(path, decoder.decode(bytes));
}
document.getElementById('files').remove();
function key(url) { return decodeURIComponent(url.pathname.slice(1)); }
window.fr5Asset = path => {
  const url = new URL(path, virtualBase);
  if (!assets.has(key(url))) throw new Error('Missing embedded asset: '+path);
  return assets.get(key(url));
};
for (const [path, css] of text) if (path.endsWith('.css')) {
  const expanded = css.replace(/url\(['"]?([^'"\)]+)['"]?\)/g, (match, value) => {
    if (/^(data:|https?:)/.test(value)) return match;
    return 'url("'+fr5Asset(new URL(value, virtualBase+path).pathname)+'")';
  });
  URL.revokeObjectURL(assets.get(path));
  assets.set(path, URL.createObjectURL(new Blob([expanded], {type:'text/css'})));
}
const reader = document.getElementById('reader');
let current, epoch = 0;
function routeString(url) { return key(url)+url.search+url.hash; }
function fileLink(url) {
  const host = new URL(window.location.href);host.hash=encodeURIComponent(routeString(url));return host.href;
}
window.fr5Replace = (url, token) => {
  if (token !== epoch) return;
  current = new URL(url);history.replaceState(null, '', fileLink(current));
};
window.fr5Route = href => {
  const url = new URL(href, current || virtualBase);
  if (url.origin !== new URL(virtualBase).origin) { window.open(url.href,'_blank','noopener');return; }
  if (!text.has(key(url)) || !key(url).endsWith('.html')) {
    window.open(fr5Asset(url.href),'_blank','noopener');return;
  }
  history.pushState(null, '', fileLink(url));show(url);
};
window.fr5Link = fileLink;
function show(url) {
  if (!text.has(key(url)) || !key(url).endsWith('.html')) url=new URL('index.html',virtualBase);
  current=url;const token=++epoch;
  const doc=new DOMParser().parseFromString(text.get(key(url)), 'text/html');
  const base=doc.createElement('base');base.href=url.href;doc.head.prepend(base);
  for (const el of doc.querySelectorAll('link[rel=stylesheet],img[src],video[src],source[src]')) {
    const attr=el.hasAttribute('href')?'href':'src';
    el.setAttribute(attr,fr5Asset(new URL(el.getAttribute(attr),url).href));
  }
  // Run the original page scripts in their original order, inside this document.
  const scripts=[];
  for (const el of doc.querySelectorAll('script[src]')) {
    const path=key(new URL(el.getAttribute('src'),url));let code=text.get(path);
    if (code===undefined) throw new Error('Missing page script: '+path);
    if (path==='observations.js') code+='\nwindow.FR5_OBSERVATIONS.forEach(row=>{row.images=row.images.map(parent.fr5Asset)});';
    if (path==='learning.js' || path==='collection.js')
      code=code.replace(/const src = (`[^`]+`);/, 'const src = parent.fr5Asset($1);');
    scripts.push('(function(){const location=window.fr5Location,history=window.fr5History;\n'+code+'\n})();');
    el.remove();
  }
  const setup=doc.createElement('script');
  setup.textContent='window.fr5Location=new URL('+JSON.stringify(url.href)+');'+
    'window.fr5History={replaceState(_s,_t,value){window.fr5Location.href=new URL(value,window.fr5Location).href;parent.fr5Replace(window.fr5Location.href,'+token+');}};';
  doc.body.append(setup);
  const app=doc.createElement('script');app.textContent=scripts.join('\n');doc.body.append(app);
  const bridge=doc.createElement('script');
  bridge.textContent=`
    function localLinks() {
      for (const a of document.querySelectorAll('a[href]')) {
        const raw=a.getAttribute('href'), u=new URL(raw,window.fr5Location);
        if (u.origin==='https://fr5.invalid') {
          a.dataset.route=u.href;a.dataset.relative=raw;
          const target=u.pathname.endsWith('.html')?parent.fr5Link(u):parent.fr5Asset(u.href);
          if(a.href!==target) a.href=target;
        } else if (!a.dataset.route && /^https?:$/.test(u.protocol)) {
          a.target='_blank';a.rel='noopener noreferrer';
        }
      }
    }
    localLinks();
    new MutationObserver(localLinks).observe(document.body,{subtree:true,childList:true,attributes:true,attributeFilter:['href']});
    document.addEventListener('click',event=>{
      const a=event.target.closest('a[data-route]');
      if (!a || event.button!==0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      event.preventDefault();parent.fr5Route(new URL(a.dataset.relative,window.fr5Location).href);
    });
    addEventListener('load',()=>{
      const target=document.getElementById(decodeURIComponent(window.fr5Location.hash.slice(1)));
      if (target) target.scrollIntoView({behavior:'instant'});
    });
  `;
  doc.body.append(bridge);
  document.title=doc.title+' · FR5 포트폴리오';
  reader.srcdoc='<!doctype html>'+doc.documentElement.outerHTML;
  reader.hidden=false;document.getElementById('loading').hidden=true;
}
function fromAddress() {
  let route;try{route=decodeURIComponent(location.hash.slice(1));}catch{route='index.html';}
  const url=new URL(route||'index.html',virtualBase);
  show(url);
}
addEventListener('popstate',fromAddress);fromAddress();
</script></html>'''


def export(output: Path) -> tuple[int, str]:
    root = Path(__file__).resolve().parent
    if output.resolve().is_relative_to(root):
        raise ValueError('Write the generated delivery outside its source directory.')
    files = {}
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.suffix in {'.py', '.pyc', '.md'}:
            continue
        mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        files[path.relative_to(root).as_posix()] = [mime, base64.b64encode(path.read_bytes()).decode('ascii')]
    for required in ('index.html', 'data.js', 'learning.js', 'connections.js', 'source-return.js'):
        assert required in files, required
    for name in ('learning.js', 'collection.js'):
        assert len(re.findall(r'const src = (`[^`]+`);', (root / name).read_text())) == 1, name
    payload = json.dumps(files, ensure_ascii=True, separators=(',', ':'))
    result = SHELL.replace('__FILES__', payload).encode('utf-8')
    assert len(result) < 50_000_000, 'Single-file delivery exceeds 50 MB'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(result)
    return len(result), hashlib.sha256(result).hexdigest()


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    size, digest = export(Path(sys.argv[1]))
    print(f'{sys.argv[1]}: {size:,} bytes; sha256 {digest}')
