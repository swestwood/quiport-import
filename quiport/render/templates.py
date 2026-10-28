"""Page shell and stylesheet.

No template engine: the export must run without a virtualenv, and the markup is
simple enough that functions returning strings are clearer than a DSL.

Every generated element carries a `quip-*` class so the whole archive can be
restyled later by editing one stylesheet.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from .. import htmlutil as H
from ..naming import path_to_root

STYLESHEET_NAME = "quip-export.css"
UTILS_NAME = "quip-utils.js"
SEARCH_INDEX_NAME = "quip-search-index.js"

# Every page's top bar. Ordered as they are used, not alphabetically.
NAV_LINKS = (
    ("index.html", "All documents"),
    ("recent.html", "Recent"),
    ("spreadsheets.html", "Spreadsheets"),
    ("slide-decks.html", "Slide decks"),
    ("needs-attention.html", "Needs attention"),
    ("limitations.html", "Limitations"),
)
HIGHLIGHT_NAME = "highlight.min.js"   # vendored highlight.js, BSD-3-Clause
ICONS_DIR = "icons"

# Thread kind -> icon file. Mirrored in quip-utils.js for JS-built rows.
ICON_BY_KIND = {
    "folder": "icon-folder.svg",
    "document": "icon-document.svg",
    "spreadsheet": "icon-spreadsheet.svg",
    "slides": "icon-slides.svg",
    "chat": "icon-chat.svg",
}

# Interface icons. Kept out of ICON_BY_KIND because that map is the set of
# *thread types*: `unknown_icon_kinds` reports any type missing from it, so a
# chrome icon in there would make a real gap look covered.
# Quip's folder colours, sampled from a screenshot of Quip's own colour picker
# (original-references/folder colors.png) rather than guessed. The API returns
# these names verbatim in `folder.color`; "manila" is the one Quip's UI labels
# "Default Color".
#
# The icon carries `fill="currentColor"` on its body path, so one sprite symbol
# serves every colour and the two black overlay paths (the flap shadow and the
# outline, at .1 and .35 opacity) sit correctly over any hue.
FOLDER_COLORS = {
    "manila":       "rgb(246, 200, 99)",
    "yellow":       "rgb(243, 177, 62)",
    "orange":       "rgb(229, 136, 69)",
    "light_orange": "rgb(240, 191, 152)",
    "red":          "rgb(236, 97, 89)",
    "light_red":    "rgb(242, 169, 165)",
    "green":        "rgb(106, 182, 83)",
    "light_green":  "rgb(172, 215, 161)",
    "blue":         "rgb(105, 151, 240)",
    "light_blue":   "rgb(174, 200, 247)",
    "purple":       "rgb(149, 93, 239)",
    "light_purple": "rgb(198, 169, 246)",
}
FOLDER_COLOR_DEFAULT = "manila"

UI_ICONS = {
    "triangle-right": "icon-triangle-right.svg",
    "comment": "icon-comment.svg",
    "code": "icon-code.svg",
}

STYLESHEET = """/* Quiport export stylesheet. Edit freely: every generated element
   carries a quip-* class, so restyling the archive is a local change. */

:root {
  --fg: #1a1a1a; --muted: #6b7280; --rule: #e5e7eb; --bg: #ffffff;
  --accent: #2563eb; --soft: #f6f7f9; --warn-bg: #fff8e1; --warn-fg: #8a6100;
}
@media (prefers-color-scheme: dark) {
  :root { --fg:#e8e8e8; --muted:#9aa3af; --rule:#2f3542; --bg:#14171c;
          --accent:#7aa7ff; --soft:#1c2027; --warn-bg:#2e2717; --warn-fg:#e8c37a; }
}
* { box-sizing: border-box; }
body { margin: 0 auto; padding: 3rem 1.5rem 6rem; max-width: 52rem;
       background: var(--bg); color: var(--fg);
       font: 16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
a { color: var(--accent); }
h1 { font-size: 1.9rem; line-height: 1.25; margin: 0 0 1rem; }
h2 { font-size: 1.3rem; margin: 2rem 0 .75rem; }
h3 { font-size: 1.05rem; margin: 1.5rem 0 .5rem; }
img { max-width: 100%; height: auto; }

/* ---- metadata block ---- */
.quip-meta { margin: 0 0 2rem; padding: .9rem 1.1rem; border-left: 3px solid var(--rule);
             background: var(--soft); color: var(--muted); font-size: .875rem; }
.quip-meta div { margin: .15rem 0; }
.quip-meta .quip-meta-label { font-weight: 600; color: var(--fg); }

/* ---- navigation ---- */
.quip-breadcrumb { font-size: .8rem; color: var(--muted); margin-bottom: 1rem; }
.quip-listing { list-style: none; padding: 0; }
.quip-listing li { padding: .35rem 0; border-bottom: 1px solid var(--rule); }
.quip-listing .quip-kind { color: var(--muted); font-size: .8rem; margin-left: .5rem; }

/* ============================================================================
   DOCUMENT PAGES -- mimic Quip's own rendering.

   Metrics below are Quip's, from the Apache-2.0 stylesheet its exporter ships
   (quip-export/lib/templates/document.css): 900px page, h1/h2 sizes, paragraph
   spacing, and its blockquote / pull-quote / table treatments. Typefaces and
   Quip's uppercase h3 label are *not* borrowed. Scoped to .quip-body
   and the content page classes so the
   archive's own chrome -- metadata block, comments, reports, index -- keeps its
   own look and is not dragged into Quip's.

   Quip is a light-only design, so content pages pin the light palette rather
   than following the system scheme. Browsing pages still adapt.
   ========================================================================== */
body.quip-document, body.quip-chat-page, body.quip-stub-page {
  --fg: #333; --muted: #6b7280; --rule: #e5e7eb; --bg: #ffffff;
  --accent: #2563eb; --soft: #f2f2f2; --warn-bg: #fff8e1; --warn-fg: #8a6100;
  background: var(--bg); color: var(--fg);
  max-width: 900px; padding: 60px 95px;
  text-rendering: optimizeLegibility; font-feature-settings: "kern";
  font-kerning: normal;
}
@media (max-width: 720px) {
  body.quip-document, body.quip-chat-page, body.quip-stub-page {
    padding: 32px 20px; }
}

/* Typeface stays the archive's own system stack. Quip sets Crimson Text and
   Roboto, but neither can be fetched from a file:// archive, and their
   fallbacks read worse than the stack they replaced -- so only Quip's sizing,
   weight and spacing are borrowed, not its font families. */
.quip-body { overflow-wrap: break-word; }

.quip-body h1, .quip-body h2, .quip-body h3, .quip-body h4, .quip-body th {
  font-weight: 700; margin: 1.25em 0 .75em; }
.quip-body h1 { font-size: 35px; line-height: 42px; }
.quip-body h1:first-child { margin-top: 0; }
.quip-body h2 { font-size: 18px; line-height: 22px; }
/* Quip sets h3/h4 as 13px uppercase labels. Not borrowed: rolled back to the
   archive's own heading sizes, so h3 still reads as a heading. */

/* Quip stores one <p class="line"> per line and represents a blank line as an
   empty paragraph, so paragraphs carry no margin of their own -- the browser
   default would double every gap and turn each blank line into three. */
.quip-body p { margin: 0; }

.quip-body blockquote, .quip-body q { display: block; margin: 1em 0;
                                      font-style: italic; }
.quip-body blockquote { padding-left: 10px; border-left: 4px solid #a6a6a6; }
.quip-body blockquote a, .quip-body q a { text-decoration: underline; }
/* <q> is Quip's centred pull quote, not an inline quotation. */
.quip-body q { color: #a6a6a6; line-height: 40px; font-size: 24px;
               text-align: center; quotes: none; }
.quip-body q::before, .quip-body q::after { content: ""; }
.quip-body q a { color: #a6a6a6; }

.quip-body code, .quip-body pre { background: #f2f2f2; }
.quip-body code { padding: 1px; margin: 0 -1px; border-radius: 3px; }
/* `white-space: pre-wrap`, NOT Quip's `nowrap`.
   Quip stores every line break inside a code block as a <br> element and never
   emits a literal newline, so `nowrap` is harmless in its own stylesheet. Here
   it is not: the moment anything replaces the block's content with real text --
   syntax highlighting does exactly that -- `nowrap` collapses the newlines and
   the whole program arrives on one line. `pre-wrap` preserves breaks from both
   representations, and matches the inline style Quip itself puts on its
   live-app dumps. */
.quip-body pre { display: block; line-height: 20px; text-shadow: 0 1px white;
                 padding: 5px 5px 5px 30px; white-space: pre-wrap;
                 overflow-wrap: anywhere; position: relative;
                 margin: 1em 0; overflow-x: auto; }
.quip-body pre::before { content: ""; position: absolute; top: 0; bottom: 0;
                         left: 15px; border-left: solid 1px #dadada; }

/* Quip's rule is a short centred divider, not a full-width line. */
.quip-body hr { width: 70px; margin: 20px auto; }

.quip-body .align-center { text-align: center; }
.quip-body .align-right { text-align: right; }
.quip-body .bold { font-weight: bold; }
.quip-body .italic { font-style: italic; }
.quip-body .underline { text-decoration: underline; }
.quip-body .strikethrough { text-decoration: line-through; }
.quip-body .underline.strikethrough { text-decoration: underline line-through; }

/* ---- Quip section styles: 5 bulleted, 6 numbered, 7 checklist,
        8/13 table, 11 image ---- */
.quip-body div[data-section-style="5"],
.quip-body div[data-section-style="6"],
.quip-body div[data-section-style="7"],
.quip-body div[data-section-style="8"],
.quip-body div[data-section-style="13"] { margin: 12px 0; }
.quip-body ul { padding: 0 0 0 40px; }
.quip-body ul li { margin-bottom: .4em; }

div[data-section-style="5"] ul { list-style-type: disc; }
div[data-section-style="5"] ul ul { list-style-type: circle; }
div[data-section-style="5"] ul ul ul { list-style-type: square; }
div[data-section-style="5"] ul ul ul ul { list-style-type: disc; }
div[data-section-style="5"] ul ul ul ul ul { list-style-type: circle; }
div[data-section-style="5"] ul ul ul ul ul ul { list-style-type: square; }
div[data-section-style="6"] ul { list-style-type: decimal; }
div[data-section-style="6"] ul ul { list-style-type: lower-alpha; }
div[data-section-style="6"] ul ul ul { list-style-type: lower-roman; }
div[data-section-style="6"] ul ul ul ul { list-style-type: decimal; }
div[data-section-style="6"] ul ul ul ul ul { list-style-type: lower-alpha; }
div[data-section-style="6"] ul ul ul ul ul ul { list-style-type: lower-roman; }

/* Checklists carry a real checkbox rather than Quip's ::before glyph, so the
   state survives with no stylesheet at all. Quip's own weighting is kept. */
.quip-checklist ul { list-style: none; padding-left: 1.25rem; }
.quip-task input[type=checkbox] { margin-right: .5rem; }
.quip-body div[data-section-style="7"] ul li.parent { font-weight: bold; }
.quip-checked { text-decoration: line-through; }
.quip-checked input[type=checkbox] { text-decoration: none; }

/* ---- tables ---- */
/* The section div is Quip's own wrapper, so it can take the horizontal scroll
   a wide sheet needs without the table having to stop being a table. */
.quip-body div[data-section-style="8"],
.quip-body div[data-section-style="13"] { overflow-x: auto; }
/* A spreadsheet may break out of the 900px page to within 24px of either
   window edge. Its box bleeds into the left margin and an equal left padding
   puts the sheet back on the text's edge; scrolling it sideways scrolls that
   padding away, so the sheet slides into the margin. The bleed is the body's
   centring margin plus its 95px padding, so this holds only for a sheet at the
   top level of the body, and only above the 720px breakpoint where that
   padding shrinks to 20px. */
@media (min-width: 721px) {
  .quip-body > div[data-section-style="13"] {
    --quip-sheet-bleed: calc(max(0px, (100vw - 900px) / 2) + 95px - 24px);
    margin-left: calc(-1 * var(--quip-sheet-bleed));
    padding-left: var(--quip-sheet-bleed);
    width: calc(100vw - 48px); }
}
.quip-body table { border-spacing: 0; border-collapse: separate;
                   border: solid 1px #bbb; table-layout: fixed; position: relative; }
.quip-body th, .quip-body td { padding: 2px 2px 0; min-width: 1.5em;
                               word-wrap: break-word; }
.quip-body th { border-bottom: 1px solid #c7cbd1; background: #f2f2f2;
                vertical-align: bottom; color: #3a4449; text-align: center; }
.quip-body td { padding-top: 0; border-left: 1px solid #c7cbd1;
                border-top: 1px solid #c7cbd1; vertical-align: top; }
/* Quip flips cell text to white once the fill is dark enough that black would
   fail against it; the export decides this in the renderer, because CSS cannot
   compute luminance. Threshold is the WCAG crossover -- see
   transform.LIGHT_TEXT_LUMINANCE. */
.quip-body td.quip-on-dark, .quip-body th.quip-on-dark { color: #fff; }
.quip-body td:first-child { border-left: hidden; }
.quip-body tr:first-child td { border-top: hidden; }

/* ---- images ---- */
.quip-body div[data-section-style="11"] { margin: 20px auto; }
.quip-body div[data-section-style="11"][data-section-float="0"] { clear: both;
                                                                  text-align: center; }
.quip-body div[data-section-style="11"][data-section-float="1"] { float: left;
                                                clear: left; margin-right: 20px; }
.quip-body div[data-section-style="11"][data-section-float="2"] { float: right;
                                                clear: right; margin-left: 20px; }
.quip-body div[data-section-style="11"] img { display: block; max-width: 100%;
                                              height: auto; margin: auto; }

/* ---- live apps ---- */
/* Quip's own <details> rendering is better than anything decoded from the
   payload, so it is shown open. It stays a <details> so a long dump can still
   be folded away. */
.quip-liveapp { border: 1px dashed var(--rule); padding: .75rem 1rem; margin: 1rem 0;
                background: var(--soft); }
.quip-liveapp-label { font-size: .8rem; color: var(--muted); margin: 0 0 .5rem;
                      text-transform: uppercase; letter-spacing: .04em; }
.quip-liveapp table { width: 100%; }
.quip-liveapp-dump > summary { cursor: pointer; list-style-position: outside; }
.quip-liveapp-dump > summary::marker { color: var(--muted); }
.quip-liveapp-dump > summary .quip-liveapp-label { margin: 0; }

/* An image well the author never filled in. Quip shows a full-size "Add an
   image" box; the archive shows a small one, because reproducing an 800x600
   hole in a document that will never be edited again is just lost space.
   Distinct from .quip-missing-image, which means a real image we could not
   fetch and which is reported for a human. */
.quip-image-placeholder { display: inline-flex; align-items: center;
    justify-content: center; width: 11rem; height: 2.25rem; margin: .5rem 0;
    border: 1px dashed #c7cbd1; border-radius: 3px; background: #fafafa;
    color: #8f9299; font-size: .8rem; user-select: none; }

/* ---- code blocks ----
   highlight.js writes hljs-* classes; these map them onto the archive's palette
   so a highlighted block still belongs to the page. Highlighting happens in the
   browser, so a reader without JavaScript sees the plain block below. */
.quip-body pre.quip-highlighted { background: #f2f2f2; }
/* The language control sits in its own bar above the block, right-aligned.
   Floating it over the top-right corner covered the first line of code. */
.quip-code-block { margin: 1em 0; }
.quip-code-bar { display: flex; align-items: center; justify-content: flex-end;
                 gap: .25rem; margin: 0 0 .15rem; opacity: .5; }
.quip-code-block:hover .quip-code-bar,
.quip-code-bar:focus-within { opacity: 1; }
.quip-code-icon { width: 13px; height: 13px; display: block; flex: 0 0 13px;
                  fill: var(--muted); }
.quip-code-lang { font: inherit; font-size: .7rem; color: var(--muted);
                  background: var(--bg); border: 1px solid var(--rule);
                  border-radius: 3px; padding: .05rem .2rem; }
.quip-code-block > pre { margin-top: 0; }
.hljs-comment, .hljs-quote { color: #6a737d; font-style: italic; }
.hljs-keyword, .hljs-selector-tag, .hljs-literal, .hljs-doctag,
.hljs-name, .hljs-section, .hljs-type { color: #005cc5; }
.hljs-string, .hljs-attr, .hljs-symbol, .hljs-bullet,
.hljs-addition, .hljs-template-tag { color: #032f62; }
.hljs-number, .hljs-regexp, .hljs-meta { color: #b31d28; }
.hljs-title, .hljs-function, .hljs-class { color: #6f42c1; }
.hljs-variable, .hljs-attribute, .hljs-built_in { color: #e36209; }
.hljs-deletion { color: #b31d28; background: #ffeef0; }
.hljs-emphasis { font-style: italic; }
.hljs-strong { font-weight: 600; }

/* ---- spreadsheet download ---- */
.quip-xlsx { margin: 0 0 1.5rem; padding: .6rem .9rem; font-size: .85rem;
             border: 1px solid var(--rule); border-radius: 4px;
             background: var(--soft); color: var(--muted); }
.quip-xlsx-link { font-weight: 600; }
.quip-xlsx-missing { border-color: var(--warn-fg); background: var(--warn-bg);
                     color: var(--warn-fg); }

/* ---- missing content ---- */
.quip-missing-image, .quip-missing-attachment {
    display: inline-block; padding: .2rem .5rem; border-radius: 3px;
    background: var(--warn-bg); color: var(--warn-fg); font-size: .85rem; }
.quip-mention-person { font-weight: 600; }
.quip-unresolved { border-bottom: 1px dotted var(--warn-fg); }
.quip-external-app-link { color: var(--muted); text-decoration: line-through dotted; }
.quip-external-app-link:hover { text-decoration: underline; }

/* ---- comments and chat ---- */
/* An anchored comment shows as a bubble in the left gutter of its line, the way
   Quip marks one, carrying the number of comments and linking to the full
   conversation at the foot of the page. It is positioned against its own line,
   so it tracks the text however the page reflows. */
.quip-has-comment { position: relative; }
.quip-comment-gutter { position: absolute; left: -2.3rem; top: 0; width: 24px;
                       height: 24px; line-height: 0; text-decoration: none; }
/* A list item's bullet or number already sits in that gutter, so its bubble
   moves further out to clear the marker -- "10." included. */
li.quip-has-comment > .quip-comment-gutter { left: -3.6rem; }
.quip-comment-bubble { width: 24px; height: 24px; display: block; fill: #8f9299; }
.quip-comment-gutter:hover .quip-comment-bubble { fill: var(--fg); }
.quip-comment-bubble text { fill: var(--fg); font-weight: 600;
                            font-size: 9px; font-family: inherit; }
.quip-comment-bubble text.quip-count-2 { font-size: 7.5px; }
.quip-comment-bubble text.quip-count-3 { font-size: 6px; }
/* Below the page's own left padding there is no gutter to sit in, so the bubble
   falls back into the flow at the start of the line it belongs to. */
@media (max-width: 720px) {
  .quip-comment-gutter { position: static; display: inline-block;
                         vertical-align: -.35rem; margin-right: .3rem; }
}
.quip-comments, .quip-chat-log { margin-top: 3rem; }
.quip-comment-context { color: var(--muted); font-size: .85rem; margin: 0 0 .35rem;
                        font-style: italic; }

/* One conversation. The quoted passage heads it, so a reader knows what is
   being discussed without following the link back. */
.quip-comment-group { margin: 0 0 1.75rem; }
.quip-comment-quote { display: block; margin-bottom: .5rem; padding-left: .75rem;
                      border-left: 3px solid var(--rule); color: var(--muted);
                      font-size: .875rem; line-height: 1.45; text-decoration: none; }
.quip-comment-quote:hover { border-left-color: var(--fg); color: var(--fg); }
.quip-comment-quote-empty { font-style: italic; }

/* No bullets: these are messages, not a list. Name and date on one line, the
   message beneath -- the shape Quip itself uses. */
.quip-comment-list, .quip-chat-list { margin: 0; padding: 0; list-style: none; }
.quip-message { margin: 0 0 .9rem; }
.quip-message:last-child { margin-bottom: 0; }
.quip-message-head { display: flex; align-items: baseline; gap: .4rem;
                     flex-wrap: wrap; line-height: 1.3; }
.quip-message-body { margin-top: .1rem; overflow-wrap: anywhere; }
.quip-author { font-weight: 600; }
.quip-timestamp { color: var(--muted); font-size: .85rem; }
/* The separator between name and date, as a pseudo-element so it is never
   selected or copied with the text. Written as the character itself rather than
   a CSS numeric escape: this stylesheet is a non-raw Python string, so Python
   decodes backslash sequences before CSS ever sees them, and the escape turned
   into a NUL byte plus stray letters. */
.quip-message-head .quip-timestamp::before { content: "·"; margin-right: .4rem; }

/* A dated rule where the day changes, as Quip does in a long transcript. */
.quip-day-divider { display: flex; align-items: center; gap: .75rem;
                    margin: 1.4rem 0 .9rem; color: var(--muted);
                    font-size: .8rem; font-weight: 600; }
.quip-day-divider::before, .quip-day-divider::after {
    content: ""; flex: 1 1 auto; border-top: 1px solid var(--rule); }
.quip-day-divider span { flex: 0 0 auto; }

.quip-comment-image { max-width: 20rem; display: block; margin: .35rem 0; }

/* ---- stubs ---- */
.quip-stub { padding: 1.25rem; background: var(--soft); border-left: 3px solid var(--accent); }

/* ---- reports ---- */
.quip-report table { border-collapse: collapse; width: 100%; font-size: .875rem;
                     display: block; overflow-x: auto; }
.quip-report th, .quip-report td { border: 1px solid var(--rule); padding: .35rem .5rem;
                                   text-align: left; vertical-align: top; }
.quip-report th { background: var(--soft); position: sticky; top: 0; }
.quip-flag { font-size: .75rem; padding: .1rem .4rem; border-radius: 3px;
             background: var(--warn-bg); color: var(--warn-fg); }
/* Icon + title inside a report table cell. inline-flex keeps the icon on the
   first line when a long title wraps. */
.quip-title-cell { display: inline-flex; align-items: flex-start; gap: .4rem; }
.quip-title-cell .quip-icon { margin-top: .15rem; }
.quip-search { width: 100%; padding: .6rem .75rem; font-size: 1rem; margin-bottom: 1rem;
               border: 1px solid var(--rule); border-radius: 4px;
               background: var(--bg); color: var(--fg); }
.quip-tree { list-style: none; padding-left: 1.15rem; }
.quip-tree li { padding: .1rem 0; }

/* One listing row: icon, title, relative date. Baseline-aligned via grid so
   long titles wrap without the icon drifting. */
.quip-entry { display: grid; grid-template-columns: 20px 1fr auto; align-items: center;
              gap: .55rem; padding: .2rem .35rem; border-radius: 4px;
              text-decoration: none; color: inherit; }
.quip-entry:hover { background: var(--soft); }
.quip-icon-sprite { position: absolute; width: 0; height: 0; overflow: hidden; }
.quip-icon { width: 20px; height: 20px; display: block; flex: 0 0 20px; }
/* Folder colours, as Quip names them in `folder.color`. The icon body is
   `currentColor`, so one sprite symbol serves all twelve. A folder whose
   colour Quip does not report keeps the manila default below. */
.quip-icon[class*="quip-folder-"] { color: rgb(246, 200, 99); }
.quip-folder-manila { color: rgb(246, 200, 99); }
.quip-folder-yellow { color: rgb(243, 177, 62); }
.quip-folder-orange { color: rgb(229, 136, 69); }
.quip-folder-light_orange { color: rgb(240, 191, 152); }
.quip-folder-red { color: rgb(236, 97, 89); }
.quip-folder-light_red { color: rgb(242, 169, 165); }
.quip-folder-green { color: rgb(106, 182, 83); }
.quip-folder-light_green { color: rgb(172, 215, 161); }
.quip-folder-blue { color: rgb(105, 151, 240); }
.quip-folder-light_blue { color: rgb(174, 200, 247); }
.quip-folder-purple { color: rgb(149, 93, 239); }
.quip-folder-light_purple { color: rgb(198, 169, 246); }
.quip-entry-title { min-width: 0; overflow-wrap: anywhere; color: var(--accent); }
.quip-folder .quip-entry-title { font-weight: 600; }
.quip-row { display: flex; align-items: center; gap: .1rem; }

/* Recents: day headings with their documents beneath, after Quip's own page.
   The two right-hand columns are fixed-width so they line up down the page
   instead of ragging against each title's length. */
.quip-day { font-size: 1.05rem; font-weight: 700; margin: 1.6rem 0 .35rem; }
.quip-day:first-of-type { margin-top: .6rem; }
.quip-recent-list { list-style: none; padding-left: 1.5rem; margin: 0; }
.quip-recent-list li { padding: 0; }
.quip-recent-row { display: flex; align-items: center; gap: .55rem;
                   padding: .3rem .35rem; border-radius: 4px; }
.quip-recent-row:hover { background: var(--rule); }
.quip-recent-row .quip-entry-title { flex: 1 1 auto; }
.quip-recent-share { flex: 0 0 7rem; color: var(--muted); font-size: .85rem; }
.quip-recent-created { flex: 0 0 7rem; color: var(--muted); font-size: .85rem;
                       text-align: right; }
.quip-recent-when { flex: 0 0 7rem; color: var(--muted); font-size: .85rem;
                    text-align: right; }
.quip-recent-count { color: var(--muted); font-size: .85rem; }

/* Site chrome: one quiet line of nav plus a search box, on every page.
   Muted and small by design -- it is scaffolding around the archive, and
   should never compete with a document's own heading. */
.quip-chrome { margin: 0 0 1.4rem; }
.quip-nav { font-size: .8rem; color: var(--muted); margin-bottom: .5rem; }
.quip-nav a { color: var(--muted); text-decoration: none; }
.quip-nav a:hover { color: var(--accent); text-decoration: underline; }
.quip-nav .quip-nav-current { color: var(--fg); font-weight: 600; }
.quip-nav-sep { opacity: .5; margin: 0 .1rem; }
.quip-searchbar .quip-search { margin-bottom: .4rem; padding: .4rem .6rem;
                               font-size: .9rem; }
.quip-filters { font-size: .8rem; color: var(--muted); }
.quip-filters summary { cursor: pointer; user-select: none; }
.quip-filter-grid { display: flex; flex-wrap: wrap; gap: .5rem .9rem;
                    margin: .5rem 0 .2rem; align-items: center; }
.quip-filter-grid label { display: flex; align-items: center; gap: .3rem; }
.quip-filter-grid input, .quip-filter-grid select, .quip-filter-grid button {
  font: inherit; padding: .2rem .35rem; border: 1px solid var(--rule);
  border-radius: 4px; background: var(--bg); color: var(--fg); }
.quip-filter-grid input[type="number"] { width: 4.5rem; }
.quip-filter-grid button { cursor: pointer; }
.quip-results { margin-top: .6rem; }
.quip-hit-count, .quip-no-hits { font-size: .8rem; color: var(--muted);
                                 margin: 0 0 .4rem; }
.quip-results[hidden] { display: none; }

/* Column headers, matching the row layout above them. */
.quip-recent-head { display: flex; align-items: center; gap: .55rem;
                    padding: .3rem .35rem; margin-top: .8rem;
                    border-bottom: 1px solid var(--rule); font-weight: 700; }
.quip-recent-head .quip-entry-title { flex: 1 1 auto; }
.quip-recent-head .quip-recent-share,
.quip-recent-head .quip-recent-created,
.quip-recent-head .quip-recent-when { color: var(--fg); font-size: .85rem; }

.quip-recent-filter { display: flex; align-items: center; gap: .5rem;
                      flex-wrap: wrap; margin: .8rem 0; font-size: .85rem;
                      color: var(--muted); }
.quip-recent-filter input, .quip-recent-filter button {
  font: inherit; padding: .25rem .45rem; border: 1px solid var(--rule);
  border-radius: 4px; background: var(--bg); color: var(--fg); }
.quip-recent-filter button { cursor: pointer; }
.quip-filter-status { color: var(--muted); }
/* [hidden] loses to `display: flex`/`list-item` without this. */
.quip-recent-item[hidden], .quip-day-group[hidden] { display: none; }

@media (max-width: 640px) {
  /* Three columns plus a title do not fit a phone. Modification date is what
     the page is ordered by, so it is the one that stays. */
  .quip-recent-share, .quip-recent-created { display: none; }
  .quip-recent-when { flex-basis: 6.5rem; }
}
.quip-row .quip-entry { flex: 1 1 auto; min-width: 0; }
/* The button keeps a comfortable hit area; the icon inside it is 12px. */
.quip-toggle { flex: 0 0 1.1rem; width: 1.1rem; height: 1.1rem; padding: 0; border: 0;
               background: none; cursor: pointer; border-radius: 3px;
               display: inline-flex; align-items: center; justify-content: center; }
.quip-toggle:hover { background: var(--soft); }
.quip-toggle:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
/* One icon for both states: collapsed points right, expanded is the same
   triangle rotated, so the two can never drift apart. pointer-events:none keeps
   every click on the button itself. */
.quip-toggle-icon { width: 12px; height: 12px; display: block; fill: rgb(51, 51, 51);
                    pointer-events: none; transition: transform .12s ease; }
.quip-toggle[aria-expanded="true"] .quip-toggle-icon { transform: rotate(90deg); }
.quip-toggle-spacer { flex: 0 0 1.1rem; width: 1.1rem; }
.quip-when { color: var(--muted); font-size: .75rem; white-space: nowrap;
             font-variant-numeric: tabular-nums; }
.quip-entry-folder { color: var(--muted); font-size: .75rem; white-space: nowrap; }
.quip-listing .quip-entry { border: 0; }
footer.quip-footer { margin-top: 4rem; padding-top: 1rem; border-top: 1px solid var(--rule);
                     color: var(--muted); font-size: .8rem; }
"""


@lru_cache(maxsize=8)
def icon_sprite(*names: str) -> str:
    """Icons as <symbol> definitions, emitted once per page.

    Every icon by default; pass names to emit only those. Document pages take
    only the comment bubble, so a page that shows one icon does not carry the
    path data for five it never uses.

    The icons are inlined rather than linked. They came from Quip's live DOM
    and carry no `xmlns` declaration, so a browser loading them through
    `<img src>` parses them as standalone XML, fails, and renders nothing.
    Inline in HTML the parser supplies the namespace and they work.

    A sprite of <symbol> definitions plus one <use> per row keeps that cheap:
    the path data appears once per page instead of once per listing entry.
    """
    parts = []
    assets = Path(__file__).resolve().parent.parent / "assets" / ICONS_DIR
    wanted = {**ICON_BY_KIND, **UI_ICONS}
    if names:
        wanted = {k: v for k, v in wanted.items() if k in names}
    for kind, filename in wanted.items():
        path = assets / filename
        if not path.exists():
            continue
        raw = path.read_text(encoding="utf-8")
        m = re.match(r"\s*<svg([^>]*)>(.*)</svg>\s*\Z", raw, re.S)
        if not m:
            continue
        attrs, inner = m.group(1), m.group(2)
        vb = re.search(r'viewBox="([^"]+)"', attrs)
        viewbox = vb.group(1) if vb else "0 0 20 20"
        parts.append(f'<symbol id="quip-icon-{kind}" viewBox="{viewbox}">{inner}</symbol>')
    if not parts:
        return ""
    return ('<svg class="quip-icon-sprite" aria-hidden="true" focusable="false" '
            'xmlns="http://www.w3.org/2000/svg">' + "".join(parts) + "</svg>")


def site_chrome(page_path: str, current: str = "") -> str:
    """The nav and search bar carried by every page.

    Deliberately quiet: one line of small links and a search box, above the
    page's own heading. It sits *outside* `.quip-body`, which is the element
    `verify`'s content check reads -- chrome inside it would be compared
    against the document's own words and reported as text that appeared from
    nowhere.

    Search needs the index, which is written once to `quip-search-index.js`
    rather than inlined: the payload is ~1 MB, and 1,592 copies of it is not a
    thing to put on disk. Filters live in a collapsed <details> so the bar
    stays a bar.
    """
    root = path_to_root(page_path)
    links = []
    for target, label in NAV_LINKS:
        href = H.escape_attr(root + target)
        cls = ' class="quip-nav-current"' if target == current else ""
        links.append(f'<a{cls} href="{href}">{H.escape_text(label)}</a>')
    types = "".join(
        f'<option value="{k}">{k.title()}</option>' for k in sorted(ICON_BY_KIND))
    return f"""<div class="quip-chrome">
<nav class="quip-nav">{' <span class="quip-nav-sep">&middot;</span> '.join(links)}</nav>
<div class="quip-searchbar">
<input id="quip-q" class="quip-search" type="search" autocomplete="off"
       placeholder="Search titles and folders&hellip;">
<details class="quip-filters"><summary>Filters</summary>
<div class="quip-filter-grid">
<label>Type <select id="quip-f-type"><option value="">Any</option>{types}</select></label>
<label>Sharing <select id="quip-f-share">
<option value="">Any</option>
<option value="private">Private only</option>
<option value="shared">Shared with someone</option>
<option value="company">Company-wide</option>
</select></label>
<label>Members at least <input id="quip-f-members" type="number" min="0" step="1"></label>
<label>Created after <input id="quip-f-created" type="date"></label>
<label>Modified after <input id="quip-f-modified" type="date"></label>
<button type="button" id="quip-f-clear">Clear filters</button>
</div></details>
</div>
<div id="quip-results" class="quip-results" hidden></div>
</div>
"""


def chrome_scripts(page_path: str) -> str:
    """The two files `site_chrome` needs, as `defer` so neither blocks paint."""
    root = path_to_root(page_path)
    return (f'<script src="{H.escape_attr(root + SEARCH_INDEX_NAME)}" defer></script>\n'
            f'<script src="{H.escape_attr(root + UTILS_NAME)}" defer></script>\n')


def icon_use(kind: str, color: str = "") -> str:
    """One icon instance, referencing the page's sprite.

    `color` is Quip's own folder colour name. An unknown or absent name simply
    leaves the default class off, so a colour Quip adds later degrades to the
    standard manila folder rather than to an uncoloured shape.
    """
    key = kind if kind in ICON_BY_KIND else "document"
    cls = "quip-icon"
    if key == "folder":
        # Always a colour class. Quip's system roots -- Private, Shared,
        # Archive, Starred -- carry no `color` field at all, and without a class
        # the icon inherits `color` from the row's link and renders blue.
        name = (color or "").lower()
        cls += f" quip-folder-{name if name in FOLDER_COLORS else FOLDER_COLOR_DEFAULT}"
    return (f'<svg class="{cls}" aria-hidden="true" focusable="false">'
            f'<use href="#quip-icon-{key}"></use></svg>')


def page(title: str, body: str, css_path: str, body_class: str = "",
         extra_head: str = "", footer: str = "") -> str:
    """Wrap rendered content in a complete, standalone HTML document."""
    cls = f' class="{H.escape_attr(body_class)}"' if body_class else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{H.escape_text(title)}</title>
<link rel="stylesheet" href="{H.escape_attr(css_path)}">
{extra_head}</head>
<body{cls}>
{body}
{footer}
</body>
</html>
"""


def footer(exported_at: str = "") -> str:
    """`exported_at` is when the API call was made, already formatted."""
    text = f"Exported from Quip on {exported_at}" if exported_at else "Exported from Quip"
    return f'<footer class="quip-footer">{H.escape_text(text)}</footer>'


def breadcrumb(parts) -> str:
    """parts: [(label, href_or_None), ...]"""
    bits = []
    for label, href in parts:
        if href:
            bits.append(f'<a href="{H.escape_attr(href)}">{H.escape_text(label)}</a>')
        else:
            bits.append(H.escape_text(label))
    return f'<nav class="quip-breadcrumb">{" / ".join(bits)}</nav>' if bits else ""
