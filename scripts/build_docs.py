#!/usr/bin/env python3
"""Render the project's Markdown documentation as browsable HTML.

    python3 scripts/build_docs.py                 # writes docs/html/*.html
    python3 scripts/build_docs.py --single PATH   # one combined page (all docs, tabbed)

The Markdown files stay the source of truth: edit them, then re-run this
script and commit the regenerated docs/html/ alongside them. The pages are
self-contained (inline CSS and JS) and open straight from disk -- no web
server or internet connection needed (the fonts fall back to system fonts
offline).

Requires the ``docs`` extra:  pip install -e ".[docs]"
(markdown-it-py renders like GitHub, so section links such as
``#1-how-it-works`` keep working, and code blocks nested in lists render).
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    from markdown_it import MarkdownIt
    from mdit_py_plugins.anchors import anchors_plugin
except ImportError:  # pragma: no cover - guidance for a missing extra
    sys.exit('markdown-it-py is required: pip install -e ".[docs]"  (or: pip install markdown-it-py mdit-py-plugins)')

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "docs" / "html"


@dataclass(frozen=True)
class Doc:
    slug: str          # output file stem, and section id in the single page
    source: str        # path relative to the repo root
    label: str         # tab label
    blurb: str         # one line shown under the title


DOCS = (
    Doc("setup-and-issues", "docs/setup-and-issues.md", "Setup & issue log",
        "Start here: step-by-step setup for a new server, and every issue met so far with its fix."),
    Doc("index", "README.md", "Reference",
        "The full reference: how printquota works, configuration, console, CLI, security and troubleshooting."),
    Doc("architecture", "docs/architecture.md", "Architecture",
        "Design decisions, the enforcement mechanism, data model and security model."),
    Doc("operations", "docs/operations.md", "Operations",
        "Day-two runbook: rollout, troubleshooting, backup and restore, upgrades, monitoring."),
    Doc("changelog", "CHANGELOG.md", "Changelog",
        "Every release and what changed in it."),
)
BY_SOURCE = {d.source: d for d in DOCS}


@dataclass
class Heading:
    level: int
    ident: str
    text: str
    children: list["Heading"] = field(default_factory=list)


@dataclass
class Rendered:
    doc: Doc
    title: str
    body: str
    toc: list[Heading]


# --------------------------------------------------------------------- render
def _markdown() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": True, "typographer": False}).enable(["table", "strikethrough"])
    md.use(anchors_plugin, min_level=1, max_level=3)
    return md


def _inline_text(token) -> str:
    return "".join(c.content for c in (token.children or []) if c.type in ("text", "code_inline"))


def _resolve_link(href: str, doc: Doc, single: bool) -> str:
    """Point a Markdown link at the right HTML page (or section of the single page)."""
    if not href or re.match(r"^[a-z][a-z0-9+.-]*:", href, re.I) or href.startswith("//"):
        return href  # external (https:, mailto:, ...)
    if href.startswith("#"):
        return f"#{_prefixed(doc, href[1:])}" if single else href
    path, _, anchor = href.partition("#")
    target = (ROOT / Path(doc.source).parent / path).resolve()
    try:
        rel = target.relative_to(ROOT).as_posix()
    except ValueError:
        return href
    other = BY_SOURCE.get(rel)
    if other is not None:
        if single:
            return f"#{_prefixed(other, anchor)}" if anchor else f"#doc-{other.slug}"
        return f"{other.slug}.html" + (f"#{anchor}" if anchor else "")
    # Any other repository file (LICENSE, scripts, ...): link to it on disk.
    return "../../" + rel + (f"#{anchor}" if anchor else "")


def _prefixed(doc: Doc, ident: str) -> str:
    return f"{doc.slug}--{ident}"


_STATUS_LI = re.compile(r"<li>((?:(?!</li>).)*?<strong>Status:</strong>(?:(?!</li>).)*?)</li>", re.S)


def _decorate_status(body: str) -> str:
    """Turn '**Status:** Fixed 0.2.4. **Verified …:**' lines into badges."""
    def badge(match: re.Match) -> str:
        inner = match.group(1)
        inner = inner.replace("<strong>Status:</strong>", '<span class="status-label">Status</span>', 1)
        inner = re.sub(r"Fixed (\d+\.\d+\.\d+)( \([^)]*\))?\.?",
                       lambda m: f'<span class="badge fixed">Fixed {m.group(1)}</span>{m.group(2) or ""}', inner)
        inner = re.sub(r"<strong>Verified( [0-9-]+)?:?</strong>",
                       lambda m: f'<span class="badge verified">Verified{m.group(1) or ""}</span>', inner)
        inner = re.sub(r"Awaiting verification on the server\.?",
                       '<span class="badge pending">Awaiting verification</span>', inner)
        inner = re.sub(r'(<span class="status-label">Status</span>)\s*Config\.',
                       r'\1 <span class="badge config">Config</span>', inner)
        inner = re.sub(r'(<span class="status-label">Status</span>)\s*Info\b( \([^)]*\))?\.?',
                       lambda m: f'{m.group(1)} <span class="badge info">Info</span>{m.group(2) or ""}', inner)
        return f'<li class="status-line">{inner}</li>'

    return _STATUS_LI.sub(badge, body)


def render(doc: Doc, single: bool) -> Rendered:
    md = _markdown()
    text = (ROOT / doc.source).read_text(encoding="utf-8")
    tokens = md.parse(text)

    title = doc.label
    flat: list[Heading] = []
    for index, token in enumerate(tokens):
        if token.type == "heading_open":
            ident = token.attrGet("id") or ""
            if single:
                ident = _prefixed(doc, ident)
                token.attrSet("id", ident)
            level = int(token.tag[1])
            label = _inline_text(tokens[index + 1])
            if level == 1:
                title = label
            else:
                flat.append(Heading(level, ident, label))
        for child in [token, *(token.children or [])]:
            if child.type == "link_open":
                child.attrSet("href", _resolve_link(child.attrGet("href") or "", doc, single))

    body = md.renderer.render(tokens, md.options, {})
    # Key/value tables written with a blank header row ("| | |") get no header.
    body = re.sub(r"<thead>\s*<tr>(?:\s*<th[^>]*>\s*</th>)+\s*</tr>\s*</thead>", "", body)
    body = re.sub(r"<table>", '<div class="table-wrap"><table>', body)
    body = body.replace("</table>", "</table></div>")
    body = _decorate_status(body)
    # Heading self-links, added after rendering so the TOC text stays clean.
    body = re.sub(
        r'<(h[23]) id="([^"]+)">(.*?)</\1>',
        lambda m: f'<{m.group(1)} id="{m.group(2)}">{m.group(3)}'
                  f'<a class="anchor" href="#{m.group(2)}" aria-label="Link to this section">#</a></{m.group(1)}>',
        body,
    )

    toc: list[Heading] = []
    for heading in flat:
        if heading.level == 2 or not toc:
            toc.append(heading)
        else:
            toc[-1].children.append(heading)
    return Rendered(doc, title, body, toc)


# ---------------------------------------------------------------------- pages
FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
         '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500'
         '&family=IBM+Plex+Sans+Condensed:wght@500;600;700&family=IBM+Plex+Sans:ital,wght@0,400;0,500;0,600;1,400'
         '&display=swap">')

CSS = r"""
:root{
  --ground:#F4F6F9; --surface:#FFFFFF; --ink:#172033; --muted:#5A6478; --faint:#8C95A6;
  --rule:#DAE0E8; --accent:#0B6E99; --accent-soft:#E3F1F8; --code:#EEF2F6; --code-ink:#1E2A3D;
  --ok:#13795B; --ok-soft:#E4F4EE; --warn:#9A5B00; --warn-soft:#FBF1E0; --info:#4B5566; --info-soft:#EDF0F4;
  --mark:#FFF3B0;
  --body:"IBM Plex Sans",-apple-system,"Segoe UI",Roboto,Ubuntu,sans-serif;
  --display:"IBM Plex Sans Condensed","Arial Narrow","Segoe UI",Roboto,sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    color-scheme:dark;
    --ground:#0F141B; --surface:#161D27; --ink:#E4E9F0; --muted:#9AA6B8; --faint:#6F7B8E;
    --rule:#2A3444; --accent:#5BB7DE; --accent-soft:#16303D; --code:#1B2431; --code-ink:#D8E1EC;
    --ok:#4CC49A; --ok-soft:#133126; --warn:#E6A94A; --warn-soft:#352812; --info:#AEB8C7; --info-soft:#232C39;
    --mark:#5A4A0A;
  }
}
:root[data-theme="dark"]{
  color-scheme:dark;
  --ground:#0F141B; --surface:#161D27; --ink:#E4E9F0; --muted:#9AA6B8; --faint:#6F7B8E;
  --rule:#2A3444; --accent:#5BB7DE; --accent-soft:#16303D; --code:#1B2431; --code-ink:#D8E1EC;
  --ok:#4CC49A; --ok-soft:#133126; --warn:#E6A94A; --warn-soft:#352812; --info:#AEB8C7; --info-soft:#232C39;
  --mark:#5A4A0A;
}
*{box-sizing:border-box}
html{scroll-padding-top:calc(76px + env(safe-area-inset-top, 0px))}
body{margin:0;background:var(--ground);color:var(--ink);font:15.5px/1.65 var(--body);-webkit-text-size-adjust:100%}
a{color:var(--accent);text-underline-offset:2px}
a:focus-visible,button:focus-visible,input:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:3px}

.topbar{position:sticky;top:env(safe-area-inset-top, 0px);z-index:10;background:var(--surface);border-bottom:1px solid var(--rule)}
.topbar-inner{max-width:1320px;margin:0 auto;padding-inline:20px;display:flex;align-items:center;gap:22px;flex-wrap:wrap;min-height:56px}
.brand{display:flex;align-items:baseline;gap:8px;text-decoration:none;color:var(--ink);padding-block:10px}
.brand b{font:700 19px/1 var(--display);letter-spacing:-.2px}
.brand span{font:500 12px/1 var(--mono);color:var(--muted)}
.tabs{display:flex;gap:4px;flex-wrap:wrap;margin:0;padding:0;list-style:none}
.tabs a{display:block;padding:17px 10px 15px;font:600 14px/1 var(--display);letter-spacing:.2px;color:var(--muted);text-decoration:none;border-bottom:2px solid transparent}
.tabs a:hover{color:var(--ink)}
.tabs a[aria-current="page"]{color:var(--ink);border-bottom-color:var(--accent)}

.layout{max-width:1320px;margin:0 auto;padding-inline:20px;padding-block:28px 64px;display:grid;grid-template-columns:270px minmax(0,1fr);gap:48px}
.side{position:sticky;top:calc(76px + env(safe-area-inset-top, 0px));align-self:start;max-height:calc(100vh - 96px);overflow:auto;padding-right:6px}
.side .find{width:100%;font:14px var(--body);color:var(--ink);background:var(--surface);border:1px solid var(--rule);border-radius:6px;padding:8px 10px;margin-bottom:12px}
.side .find::placeholder{color:var(--faint)}
.toc,.toc ul{list-style:none;margin:0;padding:0}
.toc>li{margin:2px 0}
.toc a{display:block;padding:4px 8px;border-radius:5px;color:var(--muted);text-decoration:none;font-size:13.5px;line-height:1.4}
.toc a:hover{background:var(--accent-soft);color:var(--ink)}
.toc>li>a{color:var(--ink);font-weight:500}
.toc ul{margin:2px 0 6px 10px;border-left:1px solid var(--rule);padding-left:6px}
.toc a.here{color:var(--accent);background:var(--accent-soft)}
.toc .none{color:var(--faint);font-size:13px;padding:4px 8px}

.doc{min-width:0;max-width:78ch}
.doc-head{margin-bottom:26px;padding-bottom:18px;border-bottom:1px solid var(--rule)}
.doc-head .kicker{font:500 12px/1 var(--mono);color:var(--muted);letter-spacing:.4px;text-transform:uppercase}
.doc-head p{margin:10px 0 0;color:var(--muted);max-width:65ch}
.doc h1{font:700 34px/1.15 var(--display);letter-spacing:-.4px;margin:10px 0 0;text-wrap:balance}
.doc .content>h1:first-child{display:none}
.doc h2{font:700 25px/1.25 var(--display);letter-spacing:-.2px;margin:46px 0 14px;padding-top:14px;border-top:1px solid var(--rule);text-wrap:balance}
.doc h3{font:600 19px/1.3 var(--display);margin:32px 0 10px;text-wrap:balance}
.doc h4{font:600 16px/1.35 var(--body);margin:24px 0 8px}
.doc h2 .anchor,.doc h3 .anchor{margin-left:8px;font:500 .8em var(--mono);color:var(--faint);text-decoration:none;opacity:0}
.doc h2:hover .anchor,.doc h3:hover .anchor,.doc .anchor:focus{opacity:1}
.doc p,.doc li{max-width:72ch}
.doc ul,.doc ol{padding-left:1.4em}
.doc li{margin:4px 0}
.doc li>ul,.doc li>ol{margin:4px 0}
.doc hr{border:0;border-top:1px solid var(--rule);margin:36px 0}
.doc hr:has(+ h2){display:none}
.doc blockquote{margin:18px 0;padding:10px 16px;border-left:3px solid var(--warn);background:var(--warn-soft);border-radius:0 6px 6px 0}
.doc blockquote p{margin:6px 0}
.doc code{font:13.5px/1.5 var(--mono);background:var(--code);color:var(--code-ink);padding:1px 5px;border-radius:4px;overflow-wrap:anywhere}
.doc pre{position:relative;margin:14px 0;background:var(--code);border:1px solid var(--rule);border-radius:7px;overflow-x:auto}
.doc pre code{display:block;padding:14px 16px;background:none;border-radius:0;white-space:pre;overflow-wrap:normal;font-size:13px;line-height:1.55}
.copy{position:absolute;top:7px;right:7px;font:500 12px/1 var(--body);color:var(--muted);background:var(--surface);border:1px solid var(--rule);border-radius:5px;padding:5px 9px;cursor:pointer;opacity:.9}
.copy:hover{color:var(--ink);border-color:var(--faint)}
.table-wrap{overflow-x:auto;margin:16px 0;border:1px solid var(--rule);border-radius:7px;background:var(--surface)}
.doc table{border-collapse:collapse;width:100%;font-size:14px}
.doc th,.doc td{text-align:left;vertical-align:top;padding:8px 12px;border-bottom:1px solid var(--rule)}
.doc th{font:600 12px/1.3 var(--body);text-transform:uppercase;letter-spacing:.5px;color:var(--muted);background:var(--ground)}
.doc tr:last-child td{border-bottom:0}
.doc td code{white-space:nowrap}
.status-line{list-style:none;margin-left:-1.4em !important;padding:8px 10px;background:var(--surface);border:1px solid var(--rule);border-radius:7px}
.status-label{font:600 11px/1 var(--body);text-transform:uppercase;letter-spacing:.6px;color:var(--muted);margin-right:6px}
.badge{display:inline-block;font:600 12px/1 var(--body);padding:4px 8px;border-radius:99px;margin:1px 4px 1px 0;border:1px solid transparent;white-space:nowrap}
.badge.fixed{color:var(--accent);background:var(--accent-soft)}
.badge.verified{color:var(--ok);background:var(--ok-soft)}
.badge.pending{color:var(--warn);background:var(--warn-soft)}
.badge.config,.badge.info{color:var(--info);background:var(--info-soft)}
.foot{margin-top:56px;padding-top:16px;border-top:1px solid var(--rule);color:var(--faint);font-size:13px}
mark{background:var(--mark);color:inherit;border-radius:2px}
.menu{display:none}

@media (max-width:960px){
  .layout{grid-template-columns:minmax(0,1fr);gap:18px;padding-block:18px 48px}
  .side{position:static;max-height:none;overflow:visible;padding:0;border:1px solid var(--rule);border-radius:8px;background:var(--surface)}
  .side>details>summary{list-style:none;cursor:pointer;padding:12px 14px;font:600 14px var(--display);color:var(--ink)}
  .side>details>summary::-webkit-details-marker{display:none}
  .side>details>summary::after{content:"+";float:right;color:var(--muted)}
  .side>details[open]>summary::after{content:"\2212"}
  .side .toc-body{padding:0 12px 12px;max-height:60vh;overflow:auto}
  .doc h1{font-size:28px}
  .doc h2{font-size:22px}
  .tabs a{padding:12px 8px 10px}
  html{scroll-padding-top:calc(120px + env(safe-area-inset-top, 0px))}
}
@media (min-width:961px){
  .side>details>summary{display:none}
}
@media (prefers-reduced-motion:no-preference){html{scroll-behavior:smooth}}
"""

JS = r"""
(function(){
  function copyText(btn, text){
    function done(ok){btn.textContent = ok ? 'Copied' : 'Select + Ctrl+C'; setTimeout(function(){btn.textContent='Copy';}, 1600);}
    try{
      navigator.clipboard.writeText(text).then(function(){done(true);}, function(){select(btn); done(false);});
    }catch(e){select(btn); done(false);}
  }
  function select(btn){
    var code = btn.parentNode.querySelector('code'); var r = document.createRange();
    r.selectNodeContents(code); var s = window.getSelection(); s.removeAllRanges(); s.addRange(r);
  }
  document.querySelectorAll('.doc pre').forEach(function(pre){
    var b = document.createElement('button'); b.type = 'button'; b.className = 'copy'; b.textContent = 'Copy';
    b.addEventListener('click', function(){copyText(b, pre.querySelector('code').innerText);});
    pre.appendChild(b);
  });

  // Filter the contents list by what the reader types (e.g. "TLS", "B22", "price").
  document.querySelectorAll('.side').forEach(function(side){
    var box = side.querySelector('.find'); if(!box) return;
    box.addEventListener('input', function(){
      var q = box.value.trim().toLowerCase(); var any = false;
      side.querySelectorAll('.toc>li').forEach(function(li){
        var subs = li.querySelectorAll('ul>li'); var subHit = false;
        subs.forEach(function(s){ var hit = !q || s.textContent.toLowerCase().indexOf(q) > -1; s.hidden = !hit; subHit = subHit || (hit && !!q); });
        var own = li.querySelector('a').textContent.toLowerCase().indexOf(q) > -1;
        var show = !q || own || subHit;
        if(show && own && q){ subs.forEach(function(s){ s.hidden = false; }); }
        li.hidden = !show; any = any || show;
      });
      var none = side.querySelector('.none'); if(none) none.hidden = any;
    });
  });

  // Tabs for the single-page edition: one document visible at a time.
  var sections = Array.prototype.slice.call(document.querySelectorAll('section.docsec'));
  function show(sec, target){
    sections.forEach(function(s){ s.hidden = s !== sec; });
    document.querySelectorAll('.tabs a').forEach(function(a){
      if(a.getAttribute('data-doc') === sec.id){ a.setAttribute('aria-current','page'); } else { a.removeAttribute('aria-current'); }
    });
    if(target && target !== sec){ target.scrollIntoView(); } else { window.scrollTo(0, 0); }
  }
  function route(){
    if(!sections.length) return;
    var id = decodeURIComponent(location.hash.slice(1)); var el = id && document.getElementById(id);
    if(!el){ show(sections[0]); return; }
    var sec = el.closest('section.docsec') || el; show(sec, el);
  }
  if(sections.length){ window.addEventListener('hashchange', route); route(); }

  // Highlight where the reader is in the contents list.
  if('IntersectionObserver' in window){
    var links = {}; document.querySelectorAll('.toc a').forEach(function(a){ links[a.getAttribute('href').slice(1)] = a; });
    var io = new IntersectionObserver(function(entries){
      entries.forEach(function(e){
        if(e.isIntersecting){
          document.querySelectorAll('.toc a.here').forEach(function(a){ a.classList.remove('here'); });
          var a = links[e.target.id]; if(a) a.classList.add('here');
        }
      });
    }, {rootMargin:'-80px 0px -70% 0px'});
    document.querySelectorAll('.doc h2[id], .doc h3[id]').forEach(function(h){ io.observe(h); });
  }
})();
"""


def _toc_html(toc: list[Heading]) -> str:
    def item(h: Heading) -> str:
        sub = "".join(f'<li><a href="#{html.escape(c.ident)}">{html.escape(c.text)}</a></li>' for c in h.children)
        return (f'<li><a href="#{html.escape(h.ident)}">{html.escape(h.text)}</a>'
                + (f"<ul>{sub}</ul>" if sub else "") + "</li>")
    return '<ul class="toc">' + "".join(item(h) for h in toc) + "</ul>"


def _side(r: Rendered) -> str:
    return (
        '<aside class="side" aria-label="Contents"><details open><summary>Contents</summary>'
        '<div class="toc-body">'
        f'<input class="find" type="search" id="find-{r.doc.slug}" placeholder="Filter contents (e.g. TLS, B22, price)" '
        'aria-label="Filter the contents list">'
        f'{_toc_html(r.toc)}<p class="none" hidden>No section matches.</p></div></details></aside>'
    )


def _article(r: Rendered, version: str) -> str:
    return (
        f'<main class="doc"><header class="doc-head"><div class="kicker">printquota {html.escape(version)} · '
        f'{html.escape(r.doc.label)}</div><h1>{html.escape(r.title)}</h1><p>{html.escape(r.doc.blurb)}</p></header>'
        f'<div class="content">{r.body}</div>'
        f'<footer class="foot">Generated from <code>{html.escape(r.doc.source)}</code> by '
        '<code>scripts/build_docs.py</code>. Edit the Markdown, then re-run the script.</footer></main>'
    )


def _tabs(active: Doc | None, single: bool) -> str:
    items = []
    for d in DOCS:
        href = f"#doc-{d.slug}" if single else f"{d.slug}.html"
        current = ' aria-current="page"' if (active is not None and d == active) else ""
        items.append(f'<li><a href="{href}" data-doc="doc-{d.slug}"{current}>{html.escape(d.label)}</a></li>')
    return '<ul class="tabs">' + "".join(items) + "</ul>"


def _topbar(active: Doc | None, single: bool, version: str) -> str:
    home = "#doc-setup-and-issues" if single else "setup-and-issues.html"
    return (f'<header class="topbar"><div class="topbar-inner"><a class="brand" href="{home}"><b>printquota</b>'
            f'<span>docs {html.escape(version)}</span></a><nav aria-label="Documents">{_tabs(active, single)}</nav>'
            "</div></header>")


def _version() -> str:
    text = (ROOT / "src" / "printquota" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    return match.group(1) if match else "?"


def build_site(out_dir: Path) -> list[Path]:
    version = _version()
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for doc in DOCS:
        r = render(doc, single=False)
        page = (
            "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
            f"<title>{html.escape(r.title)} · printquota</title>{FONTS}<style>{CSS}</style></head><body>"
            f"{_topbar(doc, False, version)}<div class=\"layout\">{_side(r)}{_article(r, version)}</div>"
            f"<script>{JS}</script></body></html>\n"
        )
        path = out_dir / f"{doc.slug}.html"
        path.write_text(page, encoding="utf-8", newline="\n")
        written.append(path)
    return written


def build_single(path: Path, standalone: bool = False) -> Path:
    """All documents in one page, one visible at a time (tabs)."""
    version = _version()
    sections = []
    for index, doc in enumerate(DOCS):
        r = render(doc, single=True)
        hidden = "" if index == 0 else " hidden"
        sections.append(
            f'<section class="docsec" id="doc-{doc.slug}"{hidden}><div class="layout">'
            f'{_side(r)}{_article(r, version)}</div></section>'
        )
    content = (f"<title>printquota Handbook</title>{FONTS}<style>{CSS}</style>"
               f"{_topbar(DOCS[0], True, version)}{''.join(sections)}<script>{JS}</script>")
    if standalone:
        content = ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
                   '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
                   f"</head><body>{content}</body></html>\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=OUT_DIR, help="output folder for the multi-page site")
    parser.add_argument("--single", type=Path, help="also write one combined page (fragment, for publishing)")
    parser.add_argument("--standalone", action="store_true", help="with --single: write a complete HTML file")
    args = parser.parse_args(argv)
    for path in build_site(args.out):
        print(f"wrote {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")
    if args.single:
        print(f"wrote {build_single(args.single, args.standalone)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
