"""
Deterministyczny silnik walidacji finansowej faktur oparty wyłącznie na typie Decimal.
Implementuje ścisłe reguły obliczania podatku VAT zgodnie z art. 106e ust. 10 ustawy o VAT,
rozróżniając dopuszczalne ustawowo metody (suma podstaw opodatkowania vs suma podatku z pozycji)
bez arbitralnych marginesów tolerancji.
"""

from decimal import Decimal, ROUND_HALF_UP
from typing import List, Dict, Tuple, Optional
import re

from accounting.models import (
    InvoiceSourceData,
    ValidationReport,
    DocumentStatus,
    TaxRateGroup,
    InvoiceLineItem,
    TaxGroupSummary,
)

PENNY = Decimal("0.01")

TAX_RATES_DECIMAL: Dict[TaxRateGroup, Decimal] = {
    TaxRateGroup.VAT_23: Decimal("0.23"),
    TaxRateGroup.VAT_8: Decimal("0.08"),
    TaxRateGroup.VAT_5: Decimal("0.05"),
    TaxRateGroup.VAT_0: Decimal("0.00"),
    TaxRateGroup.ZW: Decimal("0.00"),
    TaxRateGroup.NP: Decimal("0.00"),
    TaxRateGroup.OO: Decimal("0.00"),
}


def round_penny(val: Decimal) -> Decimal:
    """Zaokrąglenie księgowe do 2 miejsc po przecinku (ROUND_HALF_UP)."""
    return val.quantize(PENNY, rounding=ROUND_HALF_UP)


def validate_nip_modulo11(nip: str) -> bool:
    """
    Weryfikacja sumy kontrolnej polskiego numeru NIP (10 cyfr) algorytmem Modulo 11.
    Wagi: 6, 5, 7, 2, 3, 4, 5, 6, 7.
    """
    clean_nip = re.sub(r"[^0-9]", "", nip)
    if len(clean_nip) != 10 or clean_nip == "0000000000":
        return False

    weights = [6, 5, 7, 2, 3, 4, 5, 6, 7]
    digits = [int(d) for d in clean_nip]
    checksum = sum(w * d for w, d in zip(weights, digits[:9])) % 11

    if checksum == 10:
        return False
    return checksum == digits[9]


class InvoiceMathValidator:
    """
    Deterministyczny walidator rachunkowości faktury.
    Weryfikuje bilans netto + VAT == brutto oraz zgodność z art. 106e ust. 10 ustawy o VAT.
    """

    @classmethod
    def validate(cls, invoice: InvoiceSourceData) -> ValidationReport:
        errors: List[str] = []
        warnings: List[str] = []
        methods_matched: List[str] = []

        # 1. Walidacja NIP sprzedawcy (jeśli profil dokumentu określa polski NIP)
        if invoice.seller.is_polish_nip:
            if not validate_nip_modulo11(invoice.seller.tax_id):
                errors.append(
                    f"Błąd sumy kontrolnej Modulo 11 NIP sprzedawcy: '{invoice.seller.tax_id}'."
                )

        # 2. Walidacja obecności co najmniej 1 pozycji
        if not invoice.items:
            errors.append("Faktura nie zawiera żadnych pozycji towarowo-usługowych.")
            return ValidationReport(
                is_valid=False,
                status=DocumentStatus.REQUIRES_REVIEW,
                reconciliation_errors=errors,
                warnings=warnings,
            )

        # 3. Niezależne grupowanie pozycji wg stawek podatkowych
        items_by_rate: Dict[TaxRateGroup, List[InvoiceLineItem]] = {}
        for item in invoice.items:
            # Weryfikacja kluczowych kwot pozycji (częściowy odczyt OCR)
            if (
                item.net_amount is None
                or item.tax_rate is None
                or item.tax_amount is None
                or item.gross_amount is None
            ):
                errors.append(
                    f"Pozycja {item.position}: Niekompletne dane pozycji (brak kwoty netto, stawki VAT, podatku VAT lub brutto) uniemożliwiają matematyczne potwierdzenie."
                )
                continue

            # Weryfikacja przeliczenia pozycji: ilość * cena_netto == netto_pozycji (gdy oba pola są obecne)
            if item.quantity is not None and item.unit_price_net is not None:
                expected_item_net = round_penny(item.quantity * item.unit_price_net)
                if item.net_amount != expected_item_net:
                    errors.append(
                        f"Pozycja {item.position}: Niezgodność netto ({item.net_amount} != ilość {item.quantity} * cena {item.unit_price_net} = {expected_item_net})."
                    )

            # Weryfikacja kwoty VAT pozycji
            rate_factor = TAX_RATES_DECIMAL.get(item.tax_rate, Decimal("0.00"))
            expected_item_tax = round_penny(item.net_amount * rate_factor)
            if item.tax_amount != expected_item_tax:
                errors.append(
                    f"Pozycja {item.position}: Błędna kwota VAT ({item.tax_amount} != wyliczone {expected_item_tax} dla stawki {item.tax_rate.value})."
                )

            # Weryfikacja brutto pozycji
            expected_item_gross = item.net_amount + item.tax_amount
            if item.gross_amount != expected_item_gross:
                errors.append(
                    f"Pozycja {item.position}: Niezgodność brutto ({item.gross_amount} != netto {item.net_amount} + vat {item.tax_amount} = {expected_item_gross})."
                )

            items_by_rate.setdefault(item.tax_rate, []).append(item)

        # 4. Sprawdzenie podsumowań według grup podatkowych (Art. 106e ust. 10 ustawy o VAT)
        summaries_by_rate: Dict[TaxRateGroup, TaxGroupSummary] = {
            s.tax_rate: s for s in invoice.tax_summaries
        }

        sum_of_items_net = Decimal("0.00")
        sum_of_items_tax = Decimal("0.00")
        sum_of_items_gross = Decimal("0.00")

        for rate_group, rate_items in items_by_rate.items():
            rate_factor = TAX_RATES_DECIMAL.get(rate_group, Decimal("0.00"))
            group_net_sum = sum((it.net_amount for it in rate_items), Decimal("0.00"))
            group_tax_sum_lines = sum((it.tax_amount for it in rate_items), Decimal("0.00"))

            sum_of_items_net += group_net_sum
            sum_of_items_tax += group_tax_sum_lines
            sum_of_items_gross += sum((it.gross_amount for it in rate_items), Decimal("0.00"))

            # Dwie ustawowe metody wyliczenia podatku dla stawki:
            # Metoda 1: od sumy podstaw opodatkowania (art. 106e ust. 10 zdanie pierwsze)
            v_tax_base_method = round_penny(group_net_sum * rate_factor)
            # Metoda 2: suma podatków z poszczególnych pozycji (art. 106e ust. 10 zdanie drugie)
            v_line_items_method = group_tax_sum_lines

            # Porównanie z podsumowaniem faktury dla danej stawki
            summary = summaries_by_rate.get(rate_group)
            if not summary:
                errors.append(
                    f"Brak podsumowania dla stawki {rate_group.value} występującej w pozycjach."
                )
                continue

            if summary.net_amount != group_net_sum:
                errors.append(
                    f"Stawka {rate_group.value}: Deklarowana suma netto ({summary.net_amount}) różni się od sumy pozycji ({group_net_sum})."
                )

            # Ścisła weryfikacja kwoty podatku VAT podsumowania grupy:
            # Musi być równa v_tax_base_method LUB v_line_items_method!
            if summary.tax_amount == v_tax_base_method and summary.tax_amount == v_line_items_method:
                methods_matched.append(f"{rate_group.value}:BOTH_MATCH")
            elif summary.tax_amount == v_tax_base_method:
                methods_matched.append(f"{rate_group.value}:SUM_OF_TAX_BASES")
            elif summary.tax_amount == v_line_items_method:
                methods_matched.append(f"{rate_group.value}:SUM_OF_LINE_ITEMS")
            else:
                errors.append(
                    f"Stawka {rate_group.value}: Deklarowana kwota VAT {summary.tax_amount} jest nieprawidłowa! "
                    f"Dopuszczalne wartości wg art. 106e to {v_tax_base_method} (od sumy podstaw) lub {v_line_items_method} (z sumy pozycji)."
                )

            if summary.gross_amount != summary.net_amount + summary.tax_amount:
                errors.append(
                    f"Stawka {rate_group.value}: Niezgodność brutto w podsumowaniu ({summary.gross_amount} != {summary.net_amount} + {summary.tax_amount})."
                )

        # 5. Sprawdzenie sum całkowitych nagłówka faktury
        if invoice.total_net is None or invoice.total_tax is None or invoice.total_gross is None:
            errors.append("Brakujące sumy całkowite nagłówka faktury uniemożliwiają matematyczne potwierdzenie bilansu.")
            return ValidationReport(
                is_valid=False,
                status=DocumentStatus.REQUIRES_REVIEW,
                reconciliation_errors=errors,
                warnings=warnings,
            )

        declared_summaries_net = sum((s.net_amount for s in invoice.tax_summaries), Decimal("0.00"))
        declared_summaries_tax = sum((s.tax_amount for s in invoice.tax_summaries), Decimal("0.00"))
        declared_summaries_gross = sum((s.gross_amount for s in invoice.tax_summaries), Decimal("0.00"))

        if invoice.total_net != declared_summaries_net:
            errors.append(
                f"Łączna kwota netto nagłówka ({invoice.total_net}) != suma podsumowań stawek ({declared_summaries_net})."
            )

        if invoice.total_tax != declared_summaries_tax:
            errors.append(
                f"Łączna kwota VAT nagłówka ({invoice.total_tax}) != suma podatku ze stawek ({declared_summaries_tax})."
            )

        if invoice.total_gross != declared_summaries_gross:
            errors.append(
                f"Łączna kwota brutto nagłówka ({invoice.total_gross}) != suma brutto ze stawek ({declared_summaries_gross})."
            )

        # 6. Równanie bilansowe netto + VAT == brutto
        if invoice.total_net + invoice.total_tax != invoice.total_gross:
            errors.append(
                f"Naruszenie bilansu kwot: TotalNet ({invoice.total_net}) + TotalTax ({invoice.total_tax}) != TotalGross ({invoice.total_gross})."
            )

        is_valid = len(errors) == 0
        final_status = DocumentStatus.VALIDATED if is_valid else DocumentStatus.REQUIRES_REVIEW

        method_str = "; ".join(methods_matched) if methods_matched else None

        return ValidationReport(
            is_valid=is_valid,
            status=final_status,
            calculation_method_used=method_str,
            reconciliation_errors=errors,
            warnings=warnings,
        )
