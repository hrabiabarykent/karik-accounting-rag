import os
import pytest
from rag.parser import ACT_CODE_MAP, extract_legal_tree, build_retrieval_chunks, extract_articles_from_html
from rag.units import normalize_act, build_unit_id, parse_unit_id


def test_act_code_normalization_contract():
    """Test kontraktowy normalizacji kodów: brak emisji ORDYNACJA, kanoniczny kod OP."""
    assert normalize_act("ORDYNACJA") == "OP"
    assert normalize_act("Ordynacja Podatkowa") == "OP"
    assert normalize_act("OP") == "OP"
    assert ACT_CODE_MAP["Ordynacja_Podatkowa.html"][0] == "OP"


def test_unit_id_canonical_format():
    """Weryfikacja jednoznacznego formatu kluczowanego unit_id."""
    uid = build_unit_id("VAT", "108a", paragraph="1a")
    assert uid == "VAT:art=108a:par=1a:point=-:letter=-"

    uid2 = build_unit_id("UOR", "2", paragraph="1", point="2")
    assert uid2 == "UOR:art=2:par=1:point=2:letter=-"

    parsed = parse_unit_id(uid2)
    assert parsed["act"] == "UOR"
    assert parsed["article"] == "2"
    assert parsed["paragraph"] == "1"
    assert parsed["point"] == "2"
    assert parsed["letter"] is None


def test_parser_vat_108a_no_fake_point_122():
    """Weryfikacja: VAT Art. 108a zawiera ust. 1a i nie generuje fałszywego pkt 122 z przypisów."""
    html_path = os.path.join("data", "pobrane_ustawy", "VAT.html")
    if not os.path.exists(html_path):
        pytest.skip("Brak pliku VAT.html")

    chunks = extract_articles_from_html(html_path)
    uids_108a = [c["unit_id"] for c in chunks if c["article_number"] == "108a"]

    assert len(uids_108a) > 0
    # Obecność ust. 1a
    assert any("par=1a" in u for u in uids_108a), "Brak ust. 1a w Art. 108a VAT"
    # Wyeliminowanie fałszywego pkt 122 pochodzącego z przypisów Sejmu
    assert not any("point=122" in u for u in uids_108a), "Wykryto fałszywy pkt 122 z przypisu redakcyjnego!"


def test_parser_pit_critical_articles():
    """Weryfikacja PIT: Art. 44 ust. 6, Art. 27 ust. 1, Art. 14 ust. 1."""
    html_path = os.path.join("data", "pobrane_ustawy", "PIT.html")
    if not os.path.exists(html_path):
        pytest.skip("Brak pliku PIT.html")

    chunks = extract_articles_from_html(html_path)
    uids = {c["unit_id"]: c for c in chunks}

    # PIT Art. 44 ust. 6 (termin zaliczki do 20 dnia)
    assert any("PIT:art=44:par=6" in u for u in uids), "Brak PIT Art. 44 ust. 6"
    par6_chunk = next(c for u, c in uids.items() if "PIT:art=44:par=6" in u)
    assert "20" in par6_chunk["content"], "PIT 44 ust. 6 nie zawiera terminu 20. dnia"

    # PIT Art. 27 ust. 1 (skala podatkowa)
    assert any("PIT:art=27:par=1" in u for u in uids), "Brak PIT Art. 27 ust. 1"

    # PIT Art. 14 ust. 1 (samodzielny wpis ustępu o przychodach z działalności)
    assert any("PIT:art=14:par=1:point=-" in u for u in uids), "Brak ogólnego wpisu PIT Art. 14 ust. 1"


def test_parser_uor_critical_articles():
    """Weryfikacja UoR: Art. 2 ust. 1 pkt 2 (strukturalna i tematyczna) oraz Art. 4 ust. 1."""
    html_path = os.path.join("data", "pobrane_ustawy", "UoR_Rachunkowosc.html")
    if not os.path.exists(html_path):
        pytest.skip("Brak pliku UoR_Rachunkowosc.html")

    chunks = extract_articles_from_html(html_path)
    uids = {c["unit_id"]: c for c in chunks}

    # UOR Art. 2 ust. 1 pkt 2
    uor2_id = "UOR:art=2:par=1:point=2:letter=-"
    assert uor2_id in uids, f"Brak jednostki {uor2_id} w UoR"
    uor2_content = uids[uor2_id]["content"].lower()
    # Walidacja tematyczna (bez sztywnej kwoty):
    assert "przychody netto" in uor2_content
    assert "euro" in uor2_content

    # UOR Art. 4 ust. 1 (zasada rzetelnego i jasnego obrazu)
    uor4_id = "UOR:art=4:par=1:point=-:letter=-"
    assert uor4_id in uids, f"Brak jednostki {uor4_id} w UoR"
    assert "rzetelnie" in uids[uor4_id]["content"].lower()


def test_parser_ordynacja_podatkowa_art_193a():
    """Weryfikacja Ordynacji Podatkowej: kod OP oraz obecność Art. 193a dot. JPK."""
    html_path = os.path.join("data", "pobrane_ustawy", "Ordynacja_Podatkowa.html")
    if not os.path.exists(html_path):
        pytest.skip("Brak pliku Ordynacja_Podatkowa.html")

    chunks = extract_articles_from_html(html_path)
    op_193a = [c for c in chunks if c["act_code"] == "OP" and c["article_number"] == "193a"]

    assert len(op_193a) > 0, "Brak Art. 193a w Ordynacji Podatkowej!"
    # Wszystkie jednostki mają kod aktu OP, a nie ORDYNACJA
    for c in op_193a:
        assert c["act_code"] == "OP"
        assert not c["unit_id"].startswith("ORDYNACJA")
        assert c["unit_id"].startswith("OP:")


def test_parser_prawo_przedsiebiorcow_art_5():
    """Weryfikacja PP: Art. 5 (działalność nierejestrowana)."""
    html_path = os.path.join("data", "pobrane_ustawy", "Prawo_Przedsiebiorcow.html")
    if not os.path.exists(html_path):
        pytest.skip("Brak pliku Prawo_Przedsiebiorcow.html")

    chunks = extract_articles_from_html(html_path)
    pp_5 = [c for c in chunks if c["act_code"] == "PP" and c["article_number"] == "5"]

    assert len(pp_5) > 0, "Brak Art. 5 w Prawie Przedsiębiorców"
    par1 = next((c for c in pp_5 if c["paragraph"] == "1"), None)
    assert par1 is not None, "Brak Art. 5 ust. 1 w PP"
    assert "nie stanowi działalności gospodarczej" in par1["content"].lower()
