import pytest
from eval_rag import (
    LegalCitation,
    CitationGroup,
    match_citation,
    is_match,
    evaluate_question_citation_groups,
    evaluate_retrieval_metrics,
    normalize_act,
    normalize_article_token,
    normalize_sub_unit,
    TEST_DATASET,
)


def test_legal_citation_normalization_and_inequalities():
    """
    Weryfikacja ścisłych reguł tożsamości cytowań prawnych:
    - PIT 23 != PIT 16 (inny artykuł)
    - PIT 23 != CIT 23 (inna ustawa)
    - VAT 2 != VAT 2a (inny artykuł z literą)
    - VAT 2 != VAT 28b (brak błędu prefiksowego)
    - VAT 28 != VAT 28b
    - ORDYNACJA 193a == OP 193a (kanoniczny kod ustawy)
    - Ordynacja Podatkowa 193a == OP 193a
    """
    assert LegalCitation("PIT", "23") != LegalCitation("PIT", "16")
    assert LegalCitation("PIT", "23") != LegalCitation("CIT", "23")
    assert LegalCitation("VAT", "2") != LegalCitation("VAT", "2a")
    assert LegalCitation("VAT", "2") != LegalCitation("VAT", "28b")
    assert LegalCitation("VAT", "28") != LegalCitation("VAT", "28b")
    assert LegalCitation("ORDYNACJA", "193a") == LegalCitation("OP", "193a")
    assert LegalCitation("Ordynacja Podatkowa", "193a") == LegalCitation("OP", "193a")


def test_sub_units_with_alphanumeric_strings():
    """
    Sprawdza, czy ustęp i punkt przyjmują typ str (np. 1a, 2b),
    co jest kluczowe dla polskiej techniki prawodawczej.
    """
    cit = LegalCitation("PIT", "23", paragraph="1a", point="2b")
    assert cit.paragraph == "1a"
    assert cit.point == "2b"
    assert str(cit) == "PIT Art. 23 ust. 1a pkt 2b"

    # Sprawdzenie usuwania prefiksów 'ust.' i 'pkt'
    cit_with_prefixes = LegalCitation("CIT", "16", paragraph="ust. 5e", point="pkt 4")
    assert cit_with_prefixes.paragraph == "5e"
    assert cit_with_prefixes.point == "4"


def test_matching_levels_article_paragraph_exact():
    """
    Weryfikacja trzech poziomów dopasowania:
    - article
    - paragraph
    - exact
    """
    expected = LegalCitation("PIT", "23", paragraph="1", point="4")

    # 1. Kandydat w pełni identyczny
    cand_identical = LegalCitation("PIT", "23", paragraph="1", point="4")
    assert match_citation(cand_identical, expected, level="article") is True
    assert match_citation(cand_identical, expected, level="paragraph") is True
    assert match_citation(cand_identical, expected, level="exact") is True

    # 2. Kandydat z innym punktem (np. pkt 46 zamiast pkt 4)
    cand_diff_point = LegalCitation("PIT", "23", paragraph="1", point="46")
    assert match_citation(cand_diff_point, expected, level="article") is True
    assert match_citation(cand_diff_point, expected, level="paragraph") is True
    assert match_citation(cand_diff_point, expected, level="exact") is False

    # 3. Kandydat z innym ustępem (np. ust. 2 zamiast ust. 1)
    cand_diff_par = LegalCitation("PIT", "23", paragraph="2", point="4")
    assert match_citation(cand_diff_par, expected, level="article") is True
    assert match_citation(cand_diff_par, expected, level="paragraph") is False
    assert match_citation(cand_diff_par, expected, level="exact") is False

    # 4. Kandydat z innym artykułem (np. Art. 24 zamiast Art. 23)
    cand_diff_art = LegalCitation("PIT", "24", paragraph="1", point="4")
    assert match_citation(cand_diff_art, expected, level="article") is False
    assert match_citation(cand_diff_art, expected, level="paragraph") is False
    assert match_citation(cand_diff_art, expected, level="exact") is False

    # 5. Oczekiwane cytowanie bez punktu (np. wymagany tylko ustęp)
    exp_no_point = LegalCitation("VAT", "86a", paragraph="1")
    cand_with_point = LegalCitation("VAT", "86a", paragraph="1", point="2")
    assert match_citation(cand_with_point, exp_no_point, level="exact") is True


def test_cartesian_product_elimination():
    """
    Kluczowy test eliminacji błędu iloczynu kartezjańskiego:
    Gdy pytanie dotyczy PIT (art. 23) LUB CIT (art. 16), dawny benchmark dopasowywał:
    - (PIT, 23) -> OK
    - (CIT, 16) -> OK
    - (PIT, 16) -> BŁĄD! Fałszywe dopasowanie w iloczynie kartezjańskim!
    - (CIT, 23) -> BŁĄD! Fałszywe dopasowanie w iloczynie kartezjańskim!
    Nowy model CitationGroup z jawnym trybem odrzuca (PIT, 16) oraz (CIT, 23).
    """
    group = CitationGroup("any", [
        LegalCitation("PIT", "23"),
        LegalCitation("CIT", "16"),
    ])

    doc_pit_23 = {"act_code": "PIT", "article_number": "23"}
    doc_cit_16 = {"act_code": "CIT", "article_number": "16"}
    doc_pit_16 = {"act_code": "PIT", "article_number": "16"}  # Fałszywy rekord z dawnego kartezjana!
    doc_cit_23 = {"act_code": "CIT", "article_number": "23"}  # Fałszywy rekord z dawnego kartezjana!

    assert is_match(doc_pit_23, groups=[group]) is True
    assert is_match(doc_cit_16, groups=[group]) is True
    assert is_match(doc_pit_16, groups=[group]) is False
    assert is_match(doc_cit_23, groups=[group]) is False


def test_citation_groups_logic_any_and_all():
    """
    Testuje logikę CitationGroup dla trybów 'any' oraz 'all'.
    """
    cit_pit = LegalCitation("PIT", "22", paragraph="1")
    cit_cit = LegalCitation("CIT", "15", paragraph="1")

    grp_any = CitationGroup("any", [cit_pit, cit_cit])
    grp_all = CitationGroup("all", [cit_pit, cit_cit])

    # 1. Brak dopasowań
    no_matches = [LegalCitation("VAT", "86a")]
    ev_any_0 = grp_any.evaluate(no_matches)
    ev_all_0 = grp_all.evaluate(no_matches)
    assert ev_any_0["satisfied"] is False
    assert ev_any_0["recall"] == 0.0
    assert ev_all_0["satisfied"] is False
    assert ev_all_0["recall"] == 0.0

    # 2. Jedno dopasowanie (PIT 22)
    one_match = [LegalCitation("PIT", "22", paragraph="1")]
    ev_any_1 = grp_any.evaluate(one_match)
    ev_all_1 = grp_all.evaluate(one_match)
    assert ev_any_1["satisfied"] is True
    assert ev_any_1["recall"] == 1.0
    assert ev_all_1["satisfied"] is False
    assert ev_all_1["recall"] == 0.5

    # 3. Obydwa dopasowania (PIT 22 i CIT 15)
    both_matches = [
        LegalCitation("PIT", "22", paragraph="1"),
        LegalCitation("CIT", "15", paragraph="1")
    ]
    ev_any_2 = grp_any.evaluate(both_matches)
    ev_all_2 = grp_all.evaluate(both_matches)
    assert ev_any_2["satisfied"] is True
    assert ev_any_2["recall"] == 1.0
    assert ev_all_2["satisfied"] is True
    assert ev_all_2["recall"] == 1.0


def test_macro_recall_and_complete_answer_rate():
    """
    Weryfikacja obliczania kompletności odpowiedzi i macro citation recall.
    """
    g1 = CitationGroup("any", [LegalCitation("PIT", "23"), LegalCitation("CIT", "16")])
    g2 = CitationGroup("all", [LegalCitation("VAT", "86a"), LegalCitation("VAT", "108a")])

    # Kandydaci spełniają g1 w pełni, a g2 w połowie (znaleziono tylko 86a)
    candidates = [LegalCitation("PIT", "23"), LegalCitation("VAT", "86a")]

    eval_res = evaluate_question_citation_groups(candidates, [g1, g2])
    # g1: satisfied=True, recall=1.0
    # g2: satisfied=False, recall=0.5
    # Complete answer = 0.0 (bo nie wszystkie grupy są spełnione)
    # Macro recall = (1.0 + 0.5) / 2 = 0.75
    assert eval_res["complete_answer"] == 0.0
    assert eval_res["macro_recall"] == 0.75

    # Po dołożeniu drugiego wymaganego przepisu do g2:
    candidates.append(LegalCitation("VAT", "108a"))
    eval_res_full = evaluate_question_citation_groups(candidates, [g1, g2])
    assert eval_res_full["complete_answer"] == 1.0
    assert eval_res_full["macro_recall"] == 1.0


def test_parsing_from_doc_and_text():
    """Weryfikacja parsowania struktur ze stringa oraz słownika dokumentu."""
    doc = {
        "act_code": "PIT",
        "article_number": "23 ust. 1 pkt 4"
    }
    cit = LegalCitation.from_doc(doc)
    assert cit is not None
    assert cit.act == "PIT"
    assert cit.article == "23"
    assert cit.paragraph == "1"
    assert cit.point == "4"

    text_cit = LegalCitation.from_text("Ordynacja Podatkowa Art. 193a")
    assert text_cit is not None
    assert text_cit.act == "OP"
    assert text_cit.article == "193a"


def test_benchmark_dataset_integrity():
    """Weryfikuje poprawność schematu wszystkich 15 pytań w TEST_DATASET."""
    assert len(TEST_DATASET) == 15
    for item in TEST_DATASET:
        assert "id" in item
        assert "expected_citation_groups" in item
        groups = item["expected_citation_groups"]
        assert len(groups) > 0
        for grp in groups:
            assert isinstance(grp, CitationGroup)
            assert grp.mode in ("any", "all")
            assert len(grp.citations) > 0
            for cit in grp.citations:
                assert isinstance(cit, LegalCitation)
                assert len(cit.act) > 0
                assert len(cit.article) > 0
