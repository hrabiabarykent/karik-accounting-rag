from __future__ import annotations
import uuid
from datetime import datetime, date
from decimal import Decimal
from enum import Enum
from typing import List, Dict, Optional, Any
from pydantic import BaseModel, Field, ConfigDict


class DocumentStatus(str, Enum):
    RECEIVED = "RECEIVED"
    EXTRACTED = "EXTRACTED"
    VALIDATED = "VALIDATED"
    REQUIRES_REVIEW = "REQUIRES_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class AttemptStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class SourceType(str, Enum):
    KSEF_DETERMINISTIC = "KSEF_DETERMINISTIC"
    OCR_HYPOTHESIS = "OCR_HYPOTHESIS"
    OPERATOR_CORRECTED = "OPERATOR_CORRECTED"


class ExportStatus(str, Enum):
    EXPORT_GENERATED = "EXPORT_GENERATED"
    EXPORT_PENDING = "EXPORT_PENDING"
    EXPORT_IN_FLIGHT = "EXPORT_IN_FLIGHT"
    EXPORT_TRANSMITTED = "EXPORT_TRANSMITTED"
    EXPORT_CONFIRMED = "EXPORT_CONFIRMED"
    EXPORT_FAILED = "EXPORT_FAILED"
    EXPORT_UNKNOWN = "EXPORT_UNKNOWN"


class PrivacyStatus(str, Enum):
    CLEARED = "CLEARED"
    FAILED = "FAILED"
    BYPASSED_INTERNAL = "BYPASSED_INTERNAL"


class TaxRateGroup(str, Enum):
    VAT_23 = "23%"
    VAT_8 = "8%"
    VAT_5 = "5%"
    VAT_0 = "0%"
    ZW = "zw"
    NP = "np"
    OO = "oo"


class InvoiceLineItem(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    position: int
    description: str
    quantity: Optional[Decimal] = None
    unit: str = "szt."
    unit_price_net: Optional[Decimal] = None  # Pełna precyzja wejściowa (np. do 4-6 miejsc)
    net_amount: Optional[Decimal] = None
    tax_rate: Optional[TaxRateGroup] = None
    tax_amount: Optional[Decimal] = None
    gross_amount: Optional[Decimal] = None


class TaxGroupSummary(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    tax_rate: TaxRateGroup
    net_amount: Decimal
    tax_amount: Decimal
    gross_amount: Decimal


class InvoiceParty(BaseModel):
    name: str
    tax_id: str  # NIP lub identyfikator zagraniczny
    address: Optional[str] = None
    is_polish_nip: bool = True


class InvoiceSourceData(BaseModel):
    """
    Niezmienne dane faktury wyekstrahowane ze źródła (KSeF XML lub silnika OCR).
    AI nie ma prawa ich modyfikować ani regenerować.
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)

    invoice_number: Optional[str] = None
    issue_date: Optional[date] = None
    sale_date: Optional[date] = None
    currency: Optional[str] = None
    seller: InvoiceParty
    buyer: InvoiceParty
    items: List[InvoiceLineItem] = Field(default_factory=list)
    tax_summaries: List[TaxGroupSummary] = Field(default_factory=list)
    total_net: Optional[Decimal] = None
    total_tax: Optional[Decimal] = None
    total_gross: Optional[Decimal] = None
    bank_account: Optional[str] = None


class AISuggestions(BaseModel):
    """
    Wyłącznie sugestie analityczno-księgowe generowane przez model AI.
    Nie modyfikują ani nie nadpisują kwot źródłowych.
    """
    suggested_debit_account: Optional[str] = None   # np. "401-01" (Konto Wn)
    suggested_credit_account: Optional[str] = None  # np. "210-01" (Konto Ma)
    suggested_gtu_codes: List[str] = Field(default_factory=list)
    vat_deductibility_percent: int = 100            # np. 100% lub 50% wg art. 86a ustawy o VAT
    accounting_notes: Optional[str] = None
    confidence_score: float = 0.0


class ValidationReport(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    is_valid: bool = False
    status: DocumentStatus
    calculation_method_used: Optional[str] = None   # "SUM_OF_LINE_ITEMS" lub "SUM_OF_TAX_BASES"
    reconciliation_errors: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    validated_at: datetime = Field(default_factory=datetime.utcnow)


class Tenant(BaseModel):
    id: str
    name: str
    nip: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class TenantMembership(BaseModel):
    user_id: str
    tenant_id: str
    role: str = "accountant"  # "admin", "accountant", "auditor"


class Document(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    tenant_id: str
    original_filename: str
    storage_path: str
    mime_type: str
    sha256_hash: str
    status: DocumentStatus = DocumentStatus.RECEIVED
    approved_version_id: Optional[str] = None
    created_by_operator_id: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ProcessingAttempt(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    document_id: str
    task_id: str
    attempt_number: int = 1
    status: AttemptStatus = AttemptStatus.PENDING
    worker_id: Optional[str] = None
    lease_token: Optional[str] = None
    error_details: Optional[Dict[str, Any]] = None
    started_at: Optional[datetime] = None
    heartbeat_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class OutboxEvent(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    attempt_id: str
    aggregate_type: str = "document"
    aggregate_id: str
    event_type: str = "DOCUMENT_RECEIVED"
    payload: Dict[str, Any] = Field(default_factory=dict)
    status: str = "PENDING"  # PENDING, SENT, RETRY, FAILED
    retry_count: int = 0
    max_retries: int = 5
    next_retry_at: Optional[datetime] = None
    last_error: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    sent_at: Optional[datetime] = None


class ExtractionVersion(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    document_id: str
    attempt_id: Optional[str] = None
    version_number: int = 1
    source_type: SourceType = SourceType.KSEF_DETERMINISTIC
    parent_version_id: Optional[str] = None
    immutable_source_data: Optional[InvoiceSourceData] = None
    extraction_status: str = "SUCCESS"  # SUCCESS, PARTIAL, FAILED
    review_reason: Optional[str] = None
    ai_suggestions: AISuggestions = Field(default_factory=AISuggestions)
    validation_report: ValidationReport
    created_by_operator_id: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class OperatorReview(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    document_id: str
    target_version_id: str
    operator_id: str
    decision: DocumentStatus  # APPROVED lub REJECTED
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ERPExportEvent(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    document_id: str
    extraction_version_id: str
    erp_system: str
    idempotency_key: str
    status: ExportStatus = ExportStatus.EXPORT_GENERATED
    erp_reference_id: Optional[str] = None
    error_message: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class ERPExport(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    document_id: str
    extraction_version_id: str
    erp_system: str
    idempotency_key: str
    status: ExportStatus = ExportStatus.EXPORT_GENERATED
    erp_reference_id: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ERPExportAttempt(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    erp_export_id: str
    attempt_number: int = 1
    status: str = "PENDING"  # PENDING, SUCCESS, FAILED, TIMEOUT
    http_status: Optional[int] = None
    request_payload: Dict[str, Any] = Field(default_factory=dict)
    response_payload: Dict[str, Any] = Field(default_factory=dict)
    error_message: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
