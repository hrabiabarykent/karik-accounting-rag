"""
Testy jednostkowe deterministycznego parsera KSeF FA(2), walidatora finansowego Decimal
oraz ścisłej normalizacji artykułów prawnych (eliminacja błędu 2 == 28b oraz 2 == 2a).
"""

from decimal import Decimal
from datetime import date
import pytest

from accounting.models import (
    InvoiceSourceData,
    InvoiceParty,
    InvoiceLineItem,
    TaxGroupSummary,
    TaxRateGroup,
    DocumentStatus,
)
from validators.invoice_math import InvoiceMathValidator, validate_nip_modulo11
from ksef_parser import SafeKsefXmlParser
from eval_rag import normalize_article, is_strict_citation_match, evaluate_faithfulness


def test_nip_modulo11_validation():
    # Poprawne numery NIP
    assert validate_nip_modulo11("7740001454") is True  # PKN Orlen
    assert validate_nip_modulo11("5260250995") is True  # PKO BP
    assert validate_nip_modulo11("PL 774-000-14-54") is True

    # Niepoprawne numery NIP
    assert validate_nip_modulo11("7740001455") is False  # Zła suma kontrolna
    assert validate_nip_modulo11("123456789") is False   # 9 cyfr
    assert validate_nip_modulo11("0000000000") is False  # Błędny NIP


def test_decimal_math_validator_statutory_methods():
    """
    Testuje obie dopuszczalne w art. 106e ust. 10 ustawy o VAT metody:
    1. Metoda podstawy opodatkowania: sum(netto) * stawka
    2. Metoda sumy pozycji: sum(pozycja_netto * stawka)
    Oraz odrzucenie arbitralnego błędu groszowego niezgodnego z żadną z metod.
    """
    # Trzy pozycje po 10.33 zł netto ze stawką 23%:
    # Pozycja 1: netto 10.33, vat = 10.33 * 0.23 = 2.3759 -> 2.38 brutto = 12.71
    # Pozycja 2: netto 10.33, vat = 2.38 brutto = 12.71
    # Pozycja 3: netto 10.33, vat = 2.38 brutto = 12.71
    # Suma podatków z pozycji = 2.38 * 3 = 7.14 zł (Metoda 2)
    # Suma netto = 30.99 zł. Podatek od sumy netto = 30.99 * 0.23 = 7.1277 -> 7.13 zł (Metoda 1)
    # Obie wartości (7.13 i 7.14) są dopuszczalne prawnie!
    items = [
        InvoiceLineItem(position=1, description="P1", unit_price_net=Decimal("10.33"), net_amount=Decimal("10.33"), tax_rate=TaxRateGroup.VAT_23, tax_amount=Decimal("2.38"), gross_amount=Decimal("12.71")),
        InvoiceLineItem(position=2, description="P2", unit_price_net=Decimal("10.33"), net_amount=Decimal("10.33"), tax_rate=TaxRateGroup.VAT_23, tax_amount=Decimal("2.38"), gross_amount=Decimal("12.71")),
        InvoiceLineItem(position=3, description="P3", unit_price_net=Decimal("10.33"), net_amount=Decimal("10.33"), tax_rate=TaxRateGroup.VAT_23, tax_amount=Decimal("2.38"), gross_amount=Decimal("12.71")),
    ]

    seller = InvoiceParty(name="Sprzedawca", tax_id="7740001454")
    buyer = InvoiceParty(name="Nabywca", tax_id="5260250995")

    # Wariant A: Faktura stosująca Metodę 1 (od sumy podstaw = 7.13 zł)
    inv_method1 = InvoiceSourceData(
        invoice_number="FV/M1",
        issue_date=date.today(),
        seller=seller,
        buyer=buyer,
        items=items,
        tax_summaries=[TaxGroupSummary(tax_rate=TaxRateGroup.VAT_23, net_amount=Decimal("30.99"), tax_amount=Decimal("7.13"), gross_amount=Decimal("38.12"))],
        total_net=Decimal("30.99"),
        total_tax=Decimal("7.13"),
        total_gross=Decimal("38.12")
    )
    rep1 = InvoiceMathValidator.validate(inv_method1)
    assert rep1.is_valid is True
    assert "SUM_OF_TAX_BASES" in rep1.calculation_method_used

    # Wariant B: Faktura stosująca Metodę 2 (z sumy pozycji = 7.14 zł)
    inv_method2 = InvoiceSourceData(
        invoice_number="FV/M2",
        issue_date=date.today(),
        seller=seller,
        buyer=buyer,
        items=items,
        tax_summaries=[TaxGroupSummary(tax_rate=TaxRateGroup.VAT_23, net_amount=Decimal("30.99"), tax_amount=Decimal("7.14"), gross_amount=Decimal("38.13"))],
        total_net=Decimal("30.99"),
        total_tax=Decimal("7.14"),
        total_gross=Decimal("38.13")
    )
    rep2 = InvoiceMathValidator.validate(inv_method2)
    assert rep2.is_valid is True
    assert "SUM_OF_LINE_ITEMS" in rep2.calculation_method_used

    # Wariant C: Arbitralna kwota 7.15 zł (niezgodna z żadną z metod!) -> BŁĄD
    inv_invalid = InvoiceSourceData(
        invoice_number="FV/INVALID",
        issue_date=date.today(),
        seller=seller,
        buyer=buyer,
        items=items,
        tax_summaries=[TaxGroupSummary(tax_rate=TaxRateGroup.VAT_23, net_amount=Decimal("30.99"), tax_amount=Decimal("7.15"), gross_amount=Decimal("38.14"))],
        total_net=Decimal("30.99"),
        total_tax=Decimal("7.15"),
        total_gross=Decimal("38.14")
    )
    rep3 = InvoiceMathValidator.validate(inv_invalid)
    assert rep3.is_valid is False
    assert any("TAX_CALCULATION" in e or "Dopuszczalne wartości" in e for e in rep3.reconciliation_errors)


def test_ksef_xml_parser_fa2_valid():
    """Test poprawnego parsowania faktury podstawowej KSeF FA(2)."""
    xml_sample = b"""<?xml version="1.0" encoding="UTF-8"?>
    <Faktura xmlns="http://crd.gov.pl/wzor/2023/06/29/12648/">
        <Naglowek>
            <KodFormularza>FA</KodFormularza>
            <WariantFormularza>2</WariantFormularza>
        </Naglowek>
        <Podmiot1>
            <DaneIdentyfikacyjne>
                <NIP>7740001454</NIP>
                <PelnaNazwa>PKN ORLEN SA</PelnaNazwa>
            </DaneIdentyfikacyjne>
            <Adres><AdresPol><Ulica>Chemikow 7</Ulica></AdresPol></Adres>
        </Podmiot1>
        <Podmiot2>
            <DaneIdentyfikacyjne>
                <NIP>5260250995</NIP>
                <PelnaNazwa>PKO BP SA</PelnaNazwa>
            </DaneIdentyfikacyjne>
        </Podmiot2>
        <Fa>
            <KodWaluty>PLN</KodWaluty>
            <P_1>2026-03-01</P_1>
            <P_2>FV/2026/03/001</P_2>
            <P_6>2026-03-01</P_6>
            <RodzajFaktury>VAT</RodzajFaktury>
            <P_13_1>100.00</P_13_1>
            <P_14_1>23.00</P_14_1>
            <P_15>123.00</P_15>
            <FaWiersz>
                <NrWierszaFa>1</NrWierszaFa>
                <P_7>Paliwo Verva 98</P_7>
                <P_8A>l</P_8A>
                <P_8B>20.000</P_8B>
                <P_9A>5.00</P_9A>
                <P_11>100.00</P_11>
                <P_12>23</P_12>
            </FaWiersz>
        </Fa>
    </Faktura>
    """
    res = SafeKsefXmlParser.parse_bytes(xml_sample)
    assert res.is_valid is True
    assert res.is_supported_subset is True
    assert res.schema_version == "FA(2)"
    assert res.invoice_data.invoice_number == "FV/2026/03/001"
    assert res.invoice_data.total_net == Decimal("100.00")
    assert res.invoice_data.total_tax == Decimal("23.00")
    assert res.invoice_data.total_gross == Decimal("123.00")
    assert len(res.invoice_data.items) == 1


def test_ksef_xml_parser_unsupported_routing():
    """Korekty (KOR), waluty obce (EUR) oraz FA(3) są bezpiecznie kierowane do review."""
    # 1. Faktura korygująca KOR
    kor_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <Faktura xmlns="http://crd.gov.pl/wzor/2023/06/29/12648/">
        <Fa><RodzajFaktury>KOR</RodzajFaktury><KodWaluty>PLN</KodWaluty></Fa>
    </Faktura>
    """
    res_kor = SafeKsefXmlParser.parse_bytes(kor_xml)
    assert res_kor.is_supported_subset is False
    assert "UNSUPPORTED_DOCUMENT_TYPE_KOR" in res_kor.unsupported_reason

    # 2. Waluta EUR
    eur_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <Faktura xmlns="http://crd.gov.pl/wzor/2023/06/29/12648/">
        <Fa><RodzajFaktury>VAT</RodzajFaktury><KodWaluty>EUR</KodWaluty></Fa>
    </Faktura>
    """
    res_eur = SafeKsefXmlParser.parse_bytes(eur_xml)
    assert res_eur.is_supported_subset is False
    assert "UNSUPPORTED_CURRENCY_EUR" in res_eur.unsupported_reason

    # 3. Schemat FA(3)
    fa3_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <Faktura xmlns="http://crd.gov.pl/wzor/2025/12/31/12345/">
        <Fa><RodzajFaktury>VAT</RodzajFaktury><KodWaluty>PLN</KodWaluty></Fa>
    </Faktura>
    """
    res_fa3 = SafeKsefXmlParser.parse_bytes(fa3_xml)
    assert res_fa3.is_supported_subset is False
    assert "FA3_NOT_IMPLEMENTED" in res_fa3.unsupported_reason


def test_ksef_xxe_protection():
    """Twarde odrzucenie prób ataku XXE i wstrzyknięcia encji DTD."""
    malicious_xml = b"""<?xml version="1.0"?>
    <!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
    <Faktura xmlns="http://crd.gov.pl/wzor/2023/06/29/12648/">
        <Fa><P_2>&xxe;</P_2></Fa>
    </Faktura>
    """
    res = SafeKsefXmlParser.parse_bytes(malicious_xml)
    assert res.is_valid is False
    assert "XML_PARSE_OR_SECURITY_ERROR" in res.unsupported_reason


def test_legal_article_normalization_and_matching():
    """
    Weryfikacja eliminacji błędu startswith w ewaluacji cytowań prawnych:
    - Art. 2 != Art. 2a
    - Art. 2 != Art. 28b
    - Art. 2 != Art. 20
    - Art. 28 != Art. 28b
    """
    # Normalizacja
    assert normalize_article("2") == (2, None)
    assert normalize_article("Art. 2") == (2, None)
    assert normalize_article("Art. 2a") == (2, "a")
    assert normalize_article("Art. 28b ust. 1") == (28, "b")
    assert normalize_article("20") == (20, None)

    # Testy nierówności
    assert is_strict_citation_match("VAT", "28b", "VAT", "2") is False
    assert is_strict_citation_match("VAT", "2a", "VAT", "2") is False
    assert is_strict_citation_match("VAT", "2", "VAT", "2a") is False
    assert is_strict_citation_match("VAT", "28", "VAT", "28b") is False
    assert is_strict_citation_match("VAT", "20", "VAT", "2") is False

    # Testy poprawnych dopasowań
    assert is_strict_citation_match("VAT", "Art. 28b ust. 2", "VAT", "28b") is True
    assert is_strict_citation_match("Ustawa o VAT", "Art. 2", "VAT", "2") is True
    assert is_strict_citation_match("PIT", "Art. 21 ust. 1 pkt 67", "PIT", "21") is True


def test_faithfulness_semantic_negation():
    """
    Weryfikacja wrażliwości metryki faithfulness na odwrócenie znaczenia (negacje):
    Źródło: 'Podatnik nie może odliczyć VAT'
    Odpowiedź: 'Podatnik może odliczyć VAT'
    Wynik musi być 0.0 mimo wysokiego podobieństwa leksykalnego!
    """
    source_ctx = "Zgodnie z przepisami, podatnik nie może odliczyć podatku VAT od tego zakupu."
    contrary_answer = "Podatnik może odliczyć podatek VAT od tego zakupu w pełnej wysokości."
    claims = ["odliczenie podatku VAT"]

    score = evaluate_faithfulness(contrary_answer, source_ctx, claims)
    assert score == 0.0  # Bezwzględna kara za sprzeczność semantyczną!
