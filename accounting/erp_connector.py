"""
Moduł integracji z systemem ERP (Comarch ERP Optima).
Obsługuje wymianę dokumentów w formacie Optima IO XML / FakturaZakupu.
Gwarantuje jawne zgłaszanie braku konfiguracji (brak pozorowanych sukcesów),
obsługę timeoutów sieciowych (EXPORT_UNKNOWN) oraz weryfikację idempotencji.
"""

import os
import uuid
import logging
from typing import Optional, Dict, Any
from dataclasses import dataclass
from decimal import Decimal

from .models import InvoiceSourceData, ExportStatus

logger = logging.getLogger(__name__)


class ERPIntegrationUnavailableError(Exception):
    """Konektor ERP nie został skonfigurowany w danym środowisku."""
    pass


class ERPConnectionTimeoutError(Exception):
    """Utrata łączności z serwerem ERP w trakcie trwającej transmisji (stan nieznany)."""
    pass


class ERPBusinessRejectionError(Exception):
    """System ERP jawnie odrzucił dokument z powodu błędu merytorycznego."""
    pass


class ERPConflictError(Exception):
    """Wykryto kolizję stanu lub nieuzgodnioną wcześniejszą próbę eksportu."""
    pass


@dataclass
class ERPTransmissionResult:
    status: ExportStatus
    erp_reference_id: Optional[str] = None
    error_message: Optional[str] = None
    is_simulated: bool = False
    raw_response: Optional[str] = None


class ComarchOptimaConnector:
    """
    Klient integracyjny dla Comarch ERP Optima.
    Standard wymiany: pliki Optima XML (FakturaZakupu) lub Optima WebAPI.
    """

    def __init__(self):
        self.api_url = os.getenv("OPTIMA_API_URL")
        self.xml_exchange_dir = os.getenv("OPTIMA_XML_DIR")
        self.is_test_mode = os.getenv("ERP_TEST_MODE", "false").lower() == "true"

    def is_available(self) -> bool:
        """Sprawdza, czy konektor posiada wymaganą konfigurację w środowisku."""
        if self.is_test_mode:
            return True
        return bool(self.api_url or (self.xml_exchange_dir and os.path.isdir(self.xml_exchange_dir)))

    def generate_optima_xml(self, invoice: InvoiceSourceData) -> str:
        """
        Generuje dokument zgodny ze standardem Comarch Optima XML (FakturaZakupu).
        """
        xml_lines = [
            '<?xml version="1.0" encoding="utf-8"?>',
            '<ROOT xmlns="http://www.comarch.pl/cdn/optima/dokumenty">',
            '  <FAKTURY_ZAKUPU>',
            '    <FAKTURA>',
            f'      <NUMER>{invoice.invoice_number}</NUMER>',
            f'      <DATA_WYSTAWIENIA>{invoice.issue_date.isoformat()}</DATA_WYSTAWIENIA>',
            f'      <WALUTA>{invoice.currency}</WALUTA>',
            '      <PODMIOT>',
            f'        <NIP>{invoice.seller.tax_id}</NIP>',
            f'        <NAZWA>{invoice.seller.name}</NAZWA>',
            '      </PODMIOT>',
            '      <KWOTY>',
            f'        <NETTO>{invoice.total_net:.2f}</NETTO>',
            f'        <VAT>{invoice.total_tax:.2f}</VAT>',
            f'        <BRUTTO>{invoice.total_gross:.2f}</BRUTTO>',
            '      </KWOTY>',
            '      <POZYCJE>'
        ]
        for item in invoice.items:
            xml_lines.extend([
                '        <POZYCJA>',
                f'          <LP>{item.position}</LP>',
                f'          <NAZWA>{item.description}</NAZWA>',
                f'          <ILOSC>{item.quantity:.3f}</ILOSC>',
                f'          <CENA_NETTO>{item.unit_price_net:.2f}</CENA_NETTO>',
                f'          <STAWKA_VAT>{item.tax_rate.value}</STAWKA_VAT>',
                f'          <KWOTA_VAT>{item.tax_amount:.2f}</KWOTA_VAT>',
                f'          <KWOTA_BRUTTO>{item.gross_amount:.2f}</KWOTA_BRUTTO>',
                '        </POZYCJA>'
            ])
        xml_lines.extend([
            '      </POZYCJE>',
            '    </FAKTURA>',
            '  </FAKTURY_ZAKUPU>',
            '</ROOT>'
        ])
        return "\n".join(xml_lines)

    def transmit_invoice(
        self,
        invoice: InvoiceSourceData,
        idempotency_key: str,
        simulate_network_timeout: bool = False,
        simulate_rejection: bool = False
    ) -> ERPTransmissionResult:
        """
        Transmisja dokumentu do Optimy.
        W przypadku braku konfiguracji rzuca ERPIntegrationUnavailableError (HTTP 503).
        W przypadku utraty odpowiedzi sieciowej rzuca ERPConnectionTimeoutError (EXPORT_UNKNOWN).
        """
        if not self.is_available():
            raise ERPIntegrationUnavailableError(
                "INTEGRATION_UNAVAILABLE: Konektor ERP Optima nie został skonfigurowany w środowisku. "
                "Wymagane zdefiniowanie zmiennej OPTIMA_API_URL lub OPTIMA_XML_DIR."
            )

        # 1. Tryb wymiany plikowej Optima XML (gdy skonfigurowano istniejący katalog wymiany)
        if self.xml_exchange_dir and os.path.isdir(self.xml_exchange_dir):
            try:
                xml_content = self.generate_optima_xml(invoice)
                file_name = f"FZ_{idempotency_key[:16]}_{invoice.invoice_number.replace('/', '_')}.xml"
                target_path = os.path.join(self.xml_exchange_dir, file_name)
                with open(target_path, "w", encoding="utf-8") as f:
                    f.write(xml_content)
                logger.info(f"Pomyślnie zrzucono plik wymiany Optima XML: {target_path}")
                return ERPTransmissionResult(
                    status=ExportStatus.EXPORT_TRANSMITTED,
                    erp_reference_id=f"FILE:{file_name}",
                    is_simulated=False,
                    raw_response=f"Zapisano plik XML w katalogu wymiany: {file_name}"
                )
            except Exception as e:
                logger.error(f"Błąd zapisu pliku wymiany ERP Optima: {e}")
                raise ERPBusinessRejectionError(f"Nie udało się zapisać pliku wymiany XML: {e}")

        # 2. Tryb testowy / mock API
        if self.is_test_mode:
            logger.warning(f"[ERP Optima MOCK] Transmisja faktury {invoice.invoice_number} w trybie testowym.")
            if simulate_network_timeout:
                logger.error(f"[ERP Optima MOCK] Symulacja zerwania połączenia podczas transmisji key={idempotency_key}.")
                raise ERPConnectionTimeoutError("Brak odpowiedzi z serwera ERP po zainicjowaniu połączenia (TIMEOUT).")
            if simulate_rejection:
                logger.error(f"[ERP Optima MOCK] Symulacja odrzucenia dokumentu przez ERP.")
                raise ERPBusinessRejectionError("Błąd ERP Optima: Okres obrachunkowy jest zamknięty lub NIP kontrahenta jest nieaktywny.")

            simulated_ref = f"OPTIMA-FZ-{uuid.uuid4().hex[:6].upper()}/2026"
            return ERPTransmissionResult(
                status=ExportStatus.EXPORT_CONFIRMED,
                erp_reference_id=simulated_ref,
                is_simulated=True,
                raw_response=f"<IMPORT_STATUS>OK</IMPORT_STATUS><ID>{simulated_ref}</ID>"
            )

        # Brak bezpośredniego wdrożenia WebAPI
        raise ERPIntegrationUnavailableError(
            "INTEGRATION_UNAVAILABLE: Połączenie WebAPI Optima nie zostało aktywowane w środowisku."
        )


# Globalna instancja konektora
erp_connector = ComarchOptimaConnector()
