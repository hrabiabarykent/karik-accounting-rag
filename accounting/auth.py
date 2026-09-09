"""
Moduł uwierzytelniania i kontroli uprawnień (RBAC) w architekturze Multi-Tenant.
Tożsamość operatora i jego uprawnienia do firm pochodzą wyłącznie ze zweryfikowanego
serwerowo tokena lub sesji. Nagłówki klienta nie mogą nadawać dostępu.
"""

import os
import hmac
import hashlib
import base64
import json
import time
from typing import List, Dict, Optional
from fastapi import HTTPException, Security, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

AUTH_SECRET = os.getenv("KARIK_AUTH_SECRET")
if not AUTH_SECRET or len(AUTH_SECRET) < 32:
    if os.getenv("KARIK_ALLOW_EPHEMERAL_KEY") == "1":
        AUTH_SECRET = "ephemeral-test-secret-key-at-least-32-chars-long"
    else:
        raise RuntimeError(
            "KRYTYCZNY BŁĄD BEZPIECZEŃSTWA: Zmienna środowiskowa KARIK_AUTH_SECRET nie została skonfigurowana "
            "lub jest zbyt krótka (wymagane min. 32 znaki). Aplikacja odmawia startu."
        )

security_bearer = HTTPBearer(auto_error=False)


class AuthContext(BaseModel):
    operator_id: str
    allowed_tenants: List[str]
    roles: Dict[str, str] = {}  # tenant_id -> role ("viewer", "accountant", "auditor", "admin")

    def check_tenant_access(self, requested_tenant_id: Optional[str]) -> str:
        """
        Weryfikuje, czy operator ma uprawnienia do podanego tenanta.
        Nagłówek może jedynie wybierać tenanta spośród uprawnionych.
        """
        if not requested_tenant_id:
            if not self.allowed_tenants:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Operator nie jest przypisany do żadnej firmy."
                )
            return self.allowed_tenants[0]

        if requested_tenant_id not in self.allowed_tenants:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Brak uprawnień: operator '{self.operator_id}' nie ma dostępu do tenanta '{requested_tenant_id}'."
            )
        return requested_tenant_id

    def require_role(self, tenant_id: str, allowed_roles: List[str]) -> str:
        """
        Weryfikuje, czy operator ma rolę uprawniającą do wykonania operacji w danym tenancie.
        Dostępne role: 'viewer', 'accountant', 'auditor', 'admin'.
        Administrator firmy ma pełne uprawnienia do wszystkich akcji.
        """
        self.check_tenant_access(tenant_id)
        role = self.roles.get(tenant_id, "accountant")
        if role == "admin":
            return role
        if role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Brak uprawnień: rola '{role}' nie zezwala na tę operację. Wymagana rola: {', '.join(allowed_roles)}."
            )
        return role


def create_access_token(operator_id: str, allowed_tenants: List[str], roles: Optional[Dict[str, str]] = None, expires_in_seconds: int = 86400) -> str:
    """Tworzy kryptograficznie podpisany token sesyjny (HMAC-SHA256)."""
    payload = {
        "operator_id": operator_id,
        "allowed_tenants": allowed_tenants,
        "roles": roles or {t: "accountant" for t in allowed_tenants},
        "exp": int(time.time()) + expires_in_seconds
    }
    payload_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
    b64_payload = base64.urlsafe_b64encode(payload_bytes).decode("utf-8").rstrip("=")
    
    signature = hmac.new(AUTH_SECRET.encode("utf-8"), b64_payload.encode("utf-8"), hashlib.sha256).digest()
    b64_sig = base64.urlsafe_b64encode(signature).decode("utf-8").rstrip("=")
    
    return f"{b64_payload}.{b64_sig}"


def verify_access_token(token: str) -> AuthContext:
    """Weryfikuje podpis kryptograficzny tokena i zwraca zweryfikowany kontekst."""
    if not token or "." not in token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Nieprawidłowy format tokena autoryzacyjnego."
        )
    
    parts = token.split(".")
    if len(parts) != 2:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Uszkodzony token autoryzacyjny."
        )
    
    b64_payload, b64_sig = parts
    
    # Weryfikacja podpisu
    expected_sig = hmac.new(AUTH_SECRET.encode("utf-8"), b64_payload.encode("utf-8"), hashlib.sha256).digest()
    expected_b64_sig = base64.urlsafe_b64encode(expected_sig).decode("utf-8").rstrip("=")
    
    if not hmac.compare_digest(b64_sig, expected_b64_sig):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Błędny podpis tokena autoryzacyjnego."
        )
    
    # Dekodowanie payloadu
    try:
        padding = "=" * ((4 - len(b64_payload) % 4) % 4)
        payload_json = base64.urlsafe_b64decode(b64_payload + padding).decode("utf-8")
        payload = json.loads(payload_json)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Nie można zdekodować zawartości tokena."
        )
        
    if payload.get("exp", 0) < time.time():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token autoryzacyjny wygasł."
        )
        
    return AuthContext(
        operator_id=payload["operator_id"],
        allowed_tenants=payload.get("allowed_tenants", []),
        roles=payload.get("roles", {})
    )


async def get_current_operator(credentials: Optional[HTTPAuthorizationCredentials] = Security(security_bearer)) -> AuthContext:
    """
    Dependency FastAPI: wyciąga i weryfikuje tożsamość operatora z nagłówka Authorization: Bearer <token>.
    Jeśli brak tokena, ale skonfigurowano tryb testowy z domyślnym tokenem deweloperskim, zwraca bezpieczny mock.
    W produkcji brak prawidłowego tokena oznacza 401 Unauthorized.
    """
    if credentials and credentials.credentials:
        return verify_access_token(credentials.credentials)
    
    # Tryb deweloperski (jeśli brak tokena w teście jednostkowym bez mocka)
    dev_fallback = os.getenv("KARIK_DEV_ALLOW_ANONYMOUS", "false").lower() == "true"
    if dev_fallback:
        return AuthContext(
            operator_id="dev_operator",
            allowed_tenants=["tenant_default"],
            roles={"tenant_default": "admin"}
        )
        
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Wymagany nagłówek Authorization: Bearer <token> ze zweryfikowaną tożsamością operatora."
    )
