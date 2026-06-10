"""
PROTOTYPE (proven, not yet wired in) — Formex XML paragraph extraction for the
older CJEU eras whose HTML is too inconsistent to parse (see schema_v2_design.md).

Formex (fmx4) is CELLAR's structured legal XML. Judgment grounds are
<NP><NO.P>N</NO.P><TXT>...</TXT></NP>, with quoted material wrapped in QUOT.S /
QUOT.START blocks (whose internal NO.P must be ignored). Section titles are <TI>.

Access: resolve the ENG fmx4 manifestation URI via SPARQL, then fetch
<manifestation_uri>/DOC_1 (Accept handled by the server) to get the XML body.

Validated: cases the HTML parser produced 0–4 paragraphs for yield clean,
monotonic 1..N here:
  62003CJ0012 -> 149   62002CJ0276 -> 38   62005CJ0103 -> 34
  62003CJ0459 -> 184   62004CJ0144 -> 79   62000CJ0050 -> 49

Run: ./.venv/bin/python prototype_formex.py <CELEX>
"""
import sys
import re
import json
import time
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET

HEADERS = {"User-Agent": "Amicus-research/1.0"}
SPARQL = "https://publications.europa.eu/webapi/rdf/sparql"
XSD_STR = "^^<http://www.w3.org/2001/XMLSchema#string>"


def _sparql(q, tries=3):
    for _ in range(tries):
        try:
            data = urllib.parse.urlencode(
                {"query": q, "format": "application/sparql-results+json"}
            ).encode()
            req = urllib.request.Request(SPARQL, data=data, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)["results"]["bindings"]
        except Exception:
            time.sleep(3)
    return []


def formex_doc_url(celex):
    """ENG Formex (fmx4) document URL for a CELEX, or None if no ENG fmx4."""
    q = f'''PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
    SELECT ?m ?mt WHERE {{
      ?w cdm:resource_legal_id_celex "{celex}"{XSD_STR} .
      ?e cdm:expression_belongs_to_work ?w .
      ?e cdm:expression_uses_language <http://publications.europa.eu/resource/authority/language/ENG> .
      ?m cdm:manifestation_manifests_expression ?e . ?m cdm:manifestation_type ?mt .
    }}'''
    for r in _sparql(q):
        if r["mt"]["value"] == "fmx4":
            return r["m"]["value"] + "/DOC_1"
    return None


def _fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read().decode("utf-8", errors="ignore")


def parse_formex(xml):
    """Return [(paragraph_number, section_title, text)] from a Formex judgment.

    Grounds paragraphs are <NP><NO.P>N</NO.P><TXT>..</TXT></NP>. NPs nested inside
    QUOT.S / QUOT.START (quoted material) are skipped so their internal numbering
    doesn't pollute the judgment's own sequence. Section is the nearest preceding
    <TI> title.
    """
    root = ET.fromstring(xml)
    parents = {c: p for p in root.iter() for c in p}

    def in_quote(el):
        cur = parents.get(el)
        while cur is not None:
            if cur.tag in ("QUOT.S", "QUOT.START"):
                return True
            cur = parents.get(cur)
        return False

    def text_of(el):
        return re.sub(r"\s+", " ", " ".join(el.itertext())).strip()

    out = []
    for np in root.iter("NP"):
        if in_quote(np):
            continue
        nop, txt = np.find("NO.P"), np.find("TXT")
        if nop is not None and txt is not None and (nop.text or "").strip().isdigit():
            out.append((int(nop.text), text_of(txt)))
    return out


def main():
    celex = sys.argv[1] if len(sys.argv) > 1 else "62003CJ0012"
    url = formex_doc_url(celex)
    if not url:
        print(f"{celex}: no ENG fmx4 (no English text published)")
        return
    paras = parse_formex(_fetch(url))
    nums = [n for n, t in paras]
    print(f"{celex}: {len(paras)} paragraphs, "
          f"monotonic={nums == sorted(nums)}, max={max(nums) if nums else 0}")
    for n, t in paras[:3]:
        print(f"  [{n}] {t[:80]!r}")


if __name__ == "__main__":
    main()
