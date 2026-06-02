"""
PROTOTYPE (not wired into anything) — era-aware paragraph extractor from EUR-Lex
judgment HTML, recovering RELIABLE paragraph numbers + section tags.

Two layouts observed in EUR-Lex HTML:
  MODERN (~2000s+): grounds paragraphs carry <... id="pointN"> anchors.
  OLD: paragraphs are <p>N . TEXT</p>; sections delimited by <h2>Summary</h2>,
       <h2>Grounds</h2>, <h2>Operative part</h2> (a name="SM"/"MO"/"CO" anchors).

Goal: prove we can extract (paragraph_number, section, text) correctly across
eras — including recovering Simmenthal paras 15-16 that the current corpus lost.

Run: ./.venv/bin/python prototype_extract_paragraphs.py
(reads pre-downloaded HTML from /tmp/cjeu_html/<celex>.html)
"""

import re
import os
import glob

SECTION_MARKERS = [
    (re.compile(r'name="SM"|>\s*Summary\s*<', re.I), "summary"),
    (re.compile(r'name="MO"|>\s*Grounds\s*<', re.I), "grounds"),
    (re.compile(r'name="CO"|>\s*Operative part\s*<', re.I), "operative"),
]


def strip_tags(s):
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def section_at(html, pos, boundaries):
    """Return the section label whose marker most recently precedes pos."""
    label = "grounds"
    best = -1
    for mpos, lab in boundaries:
        if mpos <= pos and mpos > best:
            best, label = mpos, lab
    return label


def find_boundaries(html):
    out = []
    for rx, lab in SECTION_MARKERS:
        for m in rx.finditer(html):
            out.append((m.start(), lab))
    return sorted(out)


def extract_modern(html):
    """Paragraphs via id='pointN' anchors."""
    boundaries = find_boundaries(html)
    paras = []
    anchors = list(re.finditer(r'id="point(\d+)"', html))
    for i, m in enumerate(anchors):
        num = int(m.group(1))
        seg = html[m.end(): anchors[i + 1].start() if i + 1 < len(anchors) else len(html)]
        text = strip_tags(seg)
        if text:
            paras.append((num, section_at(html, m.start(), boundaries), text[:400]))
    return paras


def extract_old(html):
    """Paragraphs via <p>N . TEXT</p> inline numbering, sectioned by markers."""
    boundaries = find_boundaries(html)
    paras = []
    # <p> blocks beginning with a paragraph number. Old judgments glue the number
    # to the text ("15THESE PROVISIONS") or separate it ("1 . ALL TRADING").
    # Require the number be followed by '.', whitespace, or an uppercase letter,
    # and only keep paragraphs inside grounds/operative (skip the summary, whose
    # catchwords restart numbering).
    for m in re.finditer(r"<p[^>]*>\s*(\d{1,3})\s*\.?\s*(?=[A-Z(])(.*?)</p>", html, re.S):
        num = int(m.group(1))
        text = strip_tags(m.group(2))
        if len(text) >= 20:
            paras.append((num, section_at(html, m.start(), boundaries), text[:400]))
    return paras


def extract(html):
    if re.search(r'id="point\d+"', html):
        return "modern", extract_modern(html)
    return "old", extract_old(html)


def main():
    files = sorted(glob.glob("/tmp/cjeu_html/*.html"))
    for f in files:
        celex = os.path.basename(f)[:-5]
        html = open(f, encoding="utf-8", errors="ignore").read()
        mode, paras = extract(html)
        # report: grounds-only numbering monotonic? coverage?
        grounds = [p for p in paras if p[1] == "grounds"]
        nums = [p[0] for p in grounds]
        mono = nums == sorted(nums)
        secs = {}
        for _, s, _ in paras:
            secs[s] = secs.get(s, 0) + 1
        print(f"\n### {celex}  [{mode}]  total={len(paras)}  sections={secs}")
        print(f"    grounds paragraphs: {len(grounds)}  numbers monotonic: {mono}")
        print(f"    grounds nums: {nums[:20]}{'...' if len(nums) > 20 else ''}")
        # show a couple sample paragraphs
        for num, sec, txt in grounds[:2]:
            print(f"      [{sec} p{num}] {txt[:80]!r}")

    # SPECIAL CHECK: recover Simmenthal paras 15 & 16 (lost in current corpus)
    simm = "/tmp/cjeu_html/61977CJ0106.html"
    if os.path.exists(simm):
        _, paras = extract(open(simm, encoding="utf-8", errors="ignore").read())
        print("\n=== Simmenthal recovery check (paras 15, 16 were MISSING in corpus) ===")
        for num, sec, txt in paras:
            if sec == "grounds" and num in (15, 16):
                print(f"  p{num} [{sec}]: {txt[:110]!r}")


if __name__ == "__main__":
    main()
