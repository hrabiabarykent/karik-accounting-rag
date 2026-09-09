"""
Moduł trwałego magazynu dokumentów źródłowych (KSeF XML, skany PDF, obrazy)
z bezpiecznym streamingowym odbieraniem bajtów i ochroną przed atakiem przepełnienia pamięci.
"""

import os
import hashlib
from typing import Tuple
from fastapi import UploadFile, HTTPException, status

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB twardy limit
STORAGE_BASE_DIR = os.getenv("STORAGE_BASE_DIR", os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "storage", "documents")))


def get_document_storage_dir(tenant_id: str, document_id: str) -> str:
    path = os.path.join(STORAGE_BASE_DIR, tenant_id, document_id)
    os.makedirs(path, exist_ok=True)
    return path


async def stream_and_save_upload(file: UploadFile, tenant_id: str, document_id: str) -> Tuple[str, str, int]:
    """
    Zapisuje plik strumieniowo, zliczając bajty w locie.
    NIE ufa nagłówkowi Content-Length.
    W przypadku przekroczenia MAX_UPLOAD_BYTES:
    - natychmiast przerywa zapis,
    - usuwa plik częściowy z dysku,
    - rzuca HTTPException(413, 'Payload Too Large').
    Zwraca: (doc_storage_path, sha256_hash, total_bytes)
    """
    target_dir = get_document_storage_dir(tenant_id, document_id)
    filename = file.filename or f"{document_id}.bin"
    ext = os.path.splitext(filename)[1].lower() or ".bin"
    temp_target_path = os.path.join(target_dir, f"raw_source{ext}")

    total_bytes = 0
    hasher = hashlib.sha256()

    try:
        with open(temp_target_path, "wb") as buffer:
            chunk_size = 64 * 1024  # 64 KB bufor
            while True:
                chunk = await file.read(chunk_size)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > MAX_UPLOAD_BYTES:
                    # Przekroczono twardy limit!
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"Plik przekracza dopuszczalny limit wielkości 25 MB ({total_bytes} B)."
                    )
                hasher.update(chunk)
                buffer.write(chunk)
    except HTTPException:
        # Usuwamy plik częściowy po przekroczeniu limitu
        if os.path.exists(temp_target_path):
            try:
                os.remove(temp_target_path)
            except Exception:
                pass
        raise
    except Exception as exc:
        # Usuwamy plik częściowy przy niespodziewanym błędzie I/O
        if os.path.exists(temp_target_path):
            try:
                os.remove(temp_target_path)
            except Exception:
                pass
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Błąd zapisu pliku: {str(exc)}"
        )

    sha256_hex = hasher.hexdigest()
    return temp_target_path, sha256_hex, total_bytes
