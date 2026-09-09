"""
Bezpieczny, deterministyczny parser faktur KSeF XML z twardą ochroną przed atakami XXE.
Obsługuje jawnie zdefiniowany podzbiór produkcyjny MVP (FA(2) Standard VAT w PLN).
Korekty (KOR), zaliczki (ZAL), rozliczenia (ROZ), waluty obce oraz nieopublikowane
schematy FA(3) są bezpiecznie kierowane do weryfikacji manualnej (Fail-to-Review).
"""

import os
import re
import logging
from datetime import datetime, date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, List, Dict, Tuple, Any
from pydantic import BaseModel, ConfigDict

try:
    import defusedxml.ElementTree as defused_ET
except ImportError:
    defused_ET = None

from accounting.models import (
    InvoiceSourceData,
    InvoiceParty,
    InvoiceLineItem,
    TaxGroupSummary,
    TaxRateGroup,
)

logger = logging.getLogger(__name__)

FA2_NAMESPACE = "http://crd.gov.pl/wzor/2023/06/29/12648/"
FA3_NAMESPACE_PREFIX = "http://crd.gov.pl/wzor/2025"


class KsefParseResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    is_valid: bool
    is_supported_subset: bool
    unsupported_reason: Optional[str] = None
    invoice_data: Optional[InvoiceSourceData] = None
    raw_xml_text: str = ""
    schema_version: str = "UNKNOWN"
    errors: List[str] = []


class SafeKsefXmlParser:
    """
    Bezpieczny parser KSeF XML z wyłączeniem przetwarzania zewnętrznych encji DTD
    oraz deterministycznym mapowaniem do modelu InvoiceSourceData.
    """

    @classmethod
    def _parse_xml_safely(cls, xml_content: bytes) -> Any:
        """
        Parsuje bajty XML z twardym zakazem DTD i zewnętrznych encji (ochrona XXE).
        """
        # 1. Wstępna kontrola leksykalna pod kątem prób ataku XXE / DTD injection
        content_lower = xml_content.lower()
        if b"<!doctype" in content_lower or b"<!entity" in content_lower:
            raise ValueError("Wykryto niedozwoloną definicję DTD/ENTITY w pliku KSeF XML (potencjalny atak XXE).")

        # 2. Użycie defusedxml jeśli jest dostępny
        if defused_ET is not None:
            return defused_ET.fromstring(
                xml_content,
                forbid_dtd=True,
                forbid_entities=True,
                forbid_external=True
            )

        # 3. Fallback: lxml lub standardowy ElementTree z zablokowanym DTD
        try:
            from lxml import etree as lxml_ET
            parser = lxml_ET.XMLParser(
                resolve_entities=False,
                no_network=True,
                dtd_validation=False,
                load_dtd=False
            )
            return lxml_ET.fromstring(xml_content, parser=parser)
        except ImportError:
            import xml.etree.ElementTree as standard_ET
            return standard_ET.fromstring(xml_content)

    @classmethod
    def parse_file(cls, file_path: str) -> KsefParseResult:
        if not os.path.exists(file_path):
            return KsefParseResult(
                is_valid=False,
                is_supported_subset=False,
                unsupported_reason="FILE_NOT_FOUND",
                errors=[f"Plik {file_path} nie istnieje."]
            )

        try:
            with open(file_path, "rb") as f:
                content = f.read()
            return cls.parse_bytes(content)
        except Exception as e:
            return KsefParseResult(
                is_valid=False,
                is_supported_subset=False,
                unsupported_reason="READ_ERROR",
                errors=[str(e)]
            )

    @classmethod
    def parse_bytes(cls, xml_bytes: bytes) -> KsefParseResult:
        raw_text = xml_bytes.decode("utf-8", errors="ignore")
        try:
            root = cls._parse_xml_safely(xml_bytes)
        except Exception as e:
            logger.warning(f"Błąd bezpieczeństwa / składni XML KSeF: {e}")
            return KsefParseResult(
                is_valid=False,
                is_supported_subset=False,
                unsupported_reason="XML_PARSE_OR_SECURITY_ERROR",
                raw_xml_text=raw_text,
                errors=[str(e)]
            )

        # Rozpoznanie przestrzeni nazw
        tag = root.tag
        ns = ""
        if tag.startswith("{") and "}" in tag:
            ns = tag[1:].split("}")[0]

        schema_version = "UNKNOWN"
        if FA2_NAMESPACE in ns:
            schema_version = "FA(2)"
        elif FA3_NAMESPACE_PREFIX in ns or "2025" in ns or "2026" in ns:
            schema_version = "FA(3)"
            # FA(3) jest opublikowanym standardem MF od 01.02.2026 r., lecz obsługa tej wersji nie została zaimplementowana w parserze.
            return KsefParseResult(
                is_valid=True,
                is_supported_subset=False,
                unsupported_reason="FA3_NOT_IMPLEMENTED",
                raw_xml_text=raw_text,
                schema_version=schema_version,
                errors=["FA(3) jest oficjalnym standardem MF obowiązującym od 01.02.2026 r., lecz jego obsługa nie została zaimplementowana w bieżącej wersji parsera. Zaimplementowano podzbiór FA(2). Dokument skierowany do weryfikacji ręcznej (REQUIRES_REVIEW)."]
            )
        else:
            schema_version = f"CUSTOM_OR_LEGACY ({ns})"

        # Pomocnicza funkcja do wyszukiwania z uwzględnieniem wildcard namespace
        def find_text(path_expr: str, element=root) -> Optional[str]:
            # path_expr np. "Fa/Podmiot1/DaneIdentyfikacyjne/NIP"
            parts = path_expr.split("/")
            current = [element]
            for part in parts:
                next_level = []
                for cur in current:
                    for child in cur:
                        local_name = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                        if local_name == part:
                            next_level.append(child)
                current = next_level
                if not current:
                    return None
            return current[0].text.strip() if current and current[0].text else None

        def find_all(path_expr: str, element=root) -> List[Any]:
            parts = path_expr.split("/")
            current = [element]
            for part in parts:
                next_level = []
                for cur in current:
                    for child in cur:
                        local_name = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                        if local_name == part:
                            next_level.append(child)
                current = next_level
            return current

        # Kontrola rodzaju faktury (Standard VAT vs KOR / ZAL / ROZ)
        rodzaj_faktury = find_text("Fa/RodzajFaktury") or "VAT"
        if rodzaj_faktury != "VAT":
            return KsefParseResult(
                is_valid=True,
                is_supported_subset=False,
                unsupported_reason=f"UNSUPPORTED_DOCUMENT_TYPE_{rodzaj_faktury}",
                raw_xml_text=raw_text,
                schema_version=schema_version,
                errors=[f"Rodzaj faktury KSeF '{rodzaj_faktury}' wymaga ręcznej obsługi księgowej (poza zakresem MVP)."]
            )

        # Kontrola waluty (Standard PLN vs waluty obce)
        kod_waluty = find_text("Fa/KodWaluty") or "PLN"
        if kod_waluty != "PLN":
            return KsefParseResult(
                is_valid=True,
                is_supported_subset=False,
                unsupported_reason=f"UNSUPPORTED_CURRENCY_{kod_waluty}",
                raw_xml_text=raw_text,
                schema_version=schema_version,
                errors=[f"Faktura KSeF w walucie '{kod_waluty}' wymaga ręcznej obsługi różnic kursowych."]
            )

        # Deterministyczna ekstrakcja pól nagłówka
        invoice_number = find_text("Fa/P_2") or "BRAK_NUMERU"
        data_wystawienia_str = find_text("Fa/P_1") or str(date.today())
        data_sprzedazy_str = find_text("Fa/P_6") or data_wystawienia_str

        try:
            issue_date = datetime.strptime(data_wystawienia_str[:10], "%Y-%m-%d").date()
        except Exception:
            issue_date = date.today()

        try:
            sale_date = datetime.strptime(data_sprzedazy_str[:10], "%Y-%m-%d").date()
        except Exception:
            sale_date = issue_date

        # Sprzedawca (Podmiot1)
        sprzedawca_nip = find_text("Podmiot1/DaneIdentyfikacyjne/NIP") or ""
        sprzedawca_nazwa = (
            find_text("Podmiot1/DaneIdentyfikacyjne/PelnaNazwa")
            or find_text("Podmiot1/DaneIdentyfikacyjne/NazwaHandlowa")
            or "Sprzedawca Nieznany"
        )
        seller = InvoiceParty(
            name=sprzedawca_nazwa,
            tax_id=sprzedawca_nip,
            address=find_text("Podmiot1/Adres/AdresPol/Ulica") or "Polska",
            is_polish_nip=bool(sprzedawca_nip and len(re.sub(r"[^0-9]", "", sprzedawca_nip)) == 10)
        )

        # Nabywca (Podmiot2)
        nabywca_nip = find_text("Podmiot2/DaneIdentyfikacyjne/NIP") or ""
        nabywca_nazwa = (
            find_text("Podmiot2/DaneIdentyfikacyjne/PelnaNazwa")
            or find_text("Podmiot2/DaneIdentyfikacyjne/NazwaHandlowa")
            or "Nabywca Nieznany"
        )
        buyer = InvoiceParty(
            name=nabywca_nazwa,
            tax_id=nabywca_nip or "BRAK_NIP",
            address=find_text("Podmiot2/Adres/AdresPol/Ulica") or "",
            is_polish_nip=bool(nabywca_nip and len(re.sub(r"[^0-9]", "", nabywca_nip)) == 10)
        )

        # Ekstrakcja pozycji (FaWiersz)
        items: List[InvoiceLineItem] = []
        raw_items = find_all("Fa/FaWiersz")

        rate_map = {
            "23": TaxRateGroup.VAT_23,
            "23%": TaxRateGroup.VAT_23,
            "8": TaxRateGroup.VAT_8,
            "8%": TaxRateGroup.VAT_8,
            "5": TaxRateGroup.VAT_5,
            "5%": TaxRateGroup.VAT_5,
            "0": TaxRateGroup.VAT_0,
            "0%": TaxRateGroup.VAT_0,
            "zw": TaxRateGroup.ZW,
            "np": TaxRateGroup.NP,
            "oo": TaxRateGroup.OO,
        }

        position_idx = 1
        for row in raw_items:
            def row_text(field):
                return find_text(field, element=row)

            desc = row_text("P_7") or f"Pozycja {position_idx}"
            unit = row_text("P_8A") or "szt."
            qty_str = row_text("P_8B") or "1.000"
            price_net_str = row_text("P_9A") or "0.00"
            net_str = row_text("P_11") or "0.00"
            rate_raw = (row_text("P_12") or "23").strip().lower()

            tax_group = rate_map.get(rate_raw, TaxRateGroup.VAT_23)
            qty = Decimal(qty_str)
            price_net = Decimal(price_net_str)
            net_amt = Decimal(net_str)

            # Obliczenie VAT i brutto pozycji
            rate_dec = Decimal("0.23")
            if tax_group == TaxRateGroup.VAT_8:
                rate_dec = Decimal("0.08")
            elif tax_group == TaxRateGroup.VAT_5:
                rate_dec = Decimal("0.05")
            elif tax_group in (TaxRateGroup.VAT_0, TaxRateGroup.ZW, TaxRateGroup.NP, TaxRateGroup.OO):
                rate_dec = Decimal("0.00")

            tax_amt = (net_amt * rate_dec).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            gross_amt = net_amt + tax_amt

            items.append(
                InvoiceLineItem(
                    position=position_idx,
                    description=desc,
                    quantity=qty,
                    unit=unit,
                    unit_price_net=price_net,
                    net_amount=net_amt,
                    tax_rate=tax_group,
                    tax_amount=tax_amt,
                    gross_amount=gross_amt
                )
            )
            position_idx += 1

        # Ekstrakcja podsumowań deklarowanych w nagłówku (P_13_* i P_14_*)
        tax_summaries: List[TaxGroupSummary] = []

        # 23%
        p13_1 = find_text("Fa/P_13_1")
        p14_1 = find_text("Fa/P_14_1")
        if p13_1 is not None or p14_1 is not None:
            net = Decimal(p13_1 or "0.00")
            tax = Decimal(p14_1 or "0.00")
            tax_summaries.append(TaxGroupSummary(tax_rate=TaxRateGroup.VAT_23, net_amount=net, tax_amount=tax, gross_amount=net + tax))

        # 8%
        p13_2 = find_text("Fa/P_13_2")
        p14_2 = find_text("Fa/P_14_2")
        if p13_2 is not None or p14_2 is not None:
            net = Decimal(p13_2 or "0.00")
            tax = Decimal(p14_2 or "0.00")
            tax_summaries.append(TaxGroupSummary(tax_rate=TaxRateGroup.VAT_8, net_amount=net, tax_amount=tax, gross_amount=net + tax))

        # 5%
        p13_3 = find_text("Fa/P_13_3")
        p14_3 = find_text("Fa/P_14_3")
        if p13_3 is not None or p14_3 is not None:
            net = Decimal(p13_3 or "0.00")
            tax = Decimal(p14_3 or "0.00")
            tax_summaries.append(TaxGroupSummary(tax_rate=TaxRateGroup.VAT_5, net_amount=net, tax_amount=tax, gross_amount=net + tax))

        # 0%
        p13_6 = find_text("Fa/P_13_6_1") or find_text("Fa/P_13_6")
        if p13_6 is not None:
            net = Decimal(p13_6)
            tax_summaries.append(TaxGroupSummary(tax_rate=TaxRateGroup.VAT_0, net_amount=net, tax_amount=Decimal("0.00"), gross_amount=net))

        # zw
        p13_7 = find_text("Fa/P_13_7")
        if p13_7 is not None:
            net = Decimal(p13_7)
            tax_summaries.append(TaxGroupSummary(tax_rate=TaxRateGroup.ZW, net_amount=net, tax_amount=Decimal("0.00"), gross_amount=net))

        # Łączna kwota brutto
        p15_str = find_text("Fa/P_15")
        if p15_str:
            total_gross = Decimal(p15_str)
        else:
            total_gross = sum((s.gross_amount for s in tax_summaries), Decimal("0.00"))

        total_net = sum((s.net_amount for s in tax_summaries), Decimal("0.00"))
        total_tax = sum((s.tax_amount for s in tax_summaries), Decimal("0.00"))

        # Numer rachunku bankowego
        bank_account = find_text("Fa/Platnosc/RachunekBankowy/NrRB")

        source_data = InvoiceSourceData(
            invoice_number=invoice_number,
            issue_date=issue_date,
            sale_date=sale_date,
            currency=kod_waluty,
            seller=seller,
            buyer=buyer,
            items=items,
            tax_summaries=tax_summaries,
            total_net=total_net,
            total_tax=total_tax,
            total_gross=total_gross,
            bank_account=bank_account
        )

        return KsefParseResult(
            is_valid=True,
            is_supported_subset=True,
            invoice_data=source_data,
            raw_xml_text=raw_text,
            schema_version=schema_version
        )
