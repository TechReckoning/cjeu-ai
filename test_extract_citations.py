"""
Unit tests for the pure citation-parsing logic in extract_citations.py.
No database needed — run with: python3 test_extract_citations.py

Snippets are taken verbatim from real cjeu_paragraphs rows.
"""

from extract_citations import parse_citations, candidate_celex, detect_relation_type


def _celexes(text):
    return [c["candidate_celex"] for c in parse_citations(text)]


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f"  {name}")
    return cond


def run():
    ok = True

    # --- CELEX construction (case number -> CELEX) -------------------------
    ok &= check("C-57/94 -> 61994CJ0057", candidate_celex("C-", "57", "94") == "61994CJ0057")
    ok &= check("207/78 (classic) -> 61978CJ0207", candidate_celex(None, "207", "78") == "61978CJ0207")
    ok &= check("C-15/90 -> 61990CJ0015", candidate_celex("C-", "15", "90") == "61990CJ0015")
    ok &= check("300/04 -> 62004CJ0300 (2000s century)", candidate_celex(None, "300", "04") == "62004CJ0300")
    ok &= check("T-1/89 -> 61989TJ0001 (General Court)", candidate_celex("T-", "1", "89") == "61989TJ0001")

    # --- Modern citation with pinpoint ------------------------------------
    s = ("...must be interpreted strictly (Case C-57/94 Commission v Italy "
         "[1995] ECR I-1249, paragraph 23).")
    cs = parse_citations(s)
    ok &= check("modern: resolves C-57/94", _celexes(s) == ["61994CJ0057"])
    ok &= check("modern: pinpoint paragraph 23", cs[0]["cited_paragraph_number"] == 23)

    # --- Classic citation, 'following' cue --------------------------------
    s = ("Next, as the Court ruled in Case 135/83 Abels v Bedrijfsvereniging "
         "[1985] ECR 469, the Directive does not apply...")
    cs = parse_citations(s)
    ok &= check("classic: resolves 135/83 -> 61983CJ0135", cs[0]["candidate_celex"] == "61983CJ0135")
    ok &= check("classic: type=following (as the Court ruled)", cs[0]["relation_type"] == "following")

    # --- Distinguishing must be ANCHORED to the citation ------------------
    s = ("...unlike in Case C-15/90 Middleburgh [1991] ECR I-4655, paragraphs "
         "14 and 15, the rules which are essential...")
    cs = parse_citations(s)
    ok &= check("distinguishing: resolves C-15/90", cs[0]["candidate_celex"] == "61990CJ0015")
    ok &= check("distinguishing: type=distinguishing (unlike in Case)", cs[0]["relation_type"] == "distinguishing")
    ok &= check("distinguishing: pinpoint 14 (first of '14 and 15')", cs[0]["cited_paragraph_number"] == 14)

    # 'unlike' as generic prose with NO case nearby must NOT be a citation
    s = "...a kind of pre-retirement pension which, unlike old-age pensions, is not based on..."
    ok &= check("generic 'unlike' with no Case -> no citation", parse_citations(s) == [])

    # --- String of citations, each with its own pinpoint ------------------
    s = ("(see, in particular, Case 126/80 Salonia [1981] ECR 1563, paragraph 6; "
         "Case C-343/90 Lourenço Dias [1992] ECR I-4673, paragraph 20)")
    cs = parse_citations(s)
    ok &= check("string: two cites resolved",
                _celexes(s) == ["61980CJ0126", "61990CJ0343"])
    ok &= check("string: pinpoints 6 and 20",
                [c["cited_paragraph_number"] for c in cs] == [6, 20])
    ok &= check("string: type=see", cs[0]["relation_type"] == "see")

    # --- Joined Cases -> multiple edges -----------------------------------
    s = "Joined Cases C-267/91 and C-268/91 Keck and Mithouard [1993] ECR I-6097"
    ok &= check("joined: two CELEXes",
                _celexes(s) == ["61991CJ0267", "61991CJ0268"])

    # --- FALSE-POSITIVE GUARDS: instruments are not cases -----------------
    ok &= check("Directive 71/305 is NOT a citation",
                parse_citations("...the provisions of Directive 71/305 must be...") == [])
    ok &= check("Regulation No 1408/71 is NOT a citation",
                parse_citations("...Article 4(1)(h) of Regulation No 1408/71, which...") == [])
    ok &= check("Council Directive 67/227/EEC is NOT a citation",
                parse_citations("...the First Directive (Council Directive 67/227/EEC of 11 April 1967)...") == [])

    print()
    print("ALL PASSED" if ok else "SOME FAILED")
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if run() else 1)
