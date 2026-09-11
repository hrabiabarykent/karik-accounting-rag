import re
from typing import Optional, Union, Dict, Any

def normalize_act(act_str: str) -> str:
    """
    Ujednolica kod aktu prawnego do postaci kanonicznej:
    OP (zamiast ORDYNACJA, ORD, OP), VAT, PIT, CIT, UOR, ZUS, PP.
    Dla pozostałych/testowych kodów zachowuje ich oryginalny zapis.
    """
    if not act_str:
        return ""
    s = str(act_str).strip()
    u = s.upper()
    if "ORDYNACJ" in u or u in ("OP", "ORD"):
        return "OP"
    if u == "VAT" or ("VAT" in u and "USTAWA" in u):
        return "VAT"
    if u == "PIT" or ("PIT" in u and "USTAWA" in u):
        return "PIT"
    if u == "CIT" or ("CIT" in u and "USTAWA" in u):
        return "CIT"
    if u == "UOR" or "RACHUNKOWOŚ" in u or "RACHUNKOWOS" in u:
        return "UOR"
    if u == "ZUS" or "UBEZPIECZE" in u:
        return "ZUS"
    if u == "PP" or "PRZEDSIĘBIORC" in u:
        return "PP"
    return s


def normalize_article_token(art_str: str) -> str:
    """
    Normalizuje numer i literę artykułu (np. 'Art. 28b' -> '28b', 'Art. 23.' -> '23', '2a' -> '2a').
    Zachowuje precyzyjne rozróżnienie pomiędzy 2, 2a i 28b.
    """
    if not art_str:
        return ""
    s = str(art_str).strip().lower()
    s = re.sub(r"^art\.?\s*", "", s)
    m = re.match(r"^(\d+)\s*([a-z])?(?=\s+(?:ust|pkt|lit|poz)\b|\b|$)", s)
    if m:
        num = m.group(1)
        suffix = m.group(2) or ""
        return f"{num}{suffix}"
    return s.rstrip(".")


def normalize_sub_unit(val: Optional[Union[str, int]]) -> Optional[str]:
    """Normalizuje numer ustępu, punktu lub litery (np. 'ust. 1' -> '1', '1a' -> '1a', 'pkt 4' -> '4')."""
    if val is None:
        return None
    s = str(val).strip().lower()
    s = re.sub(r"^(?:ust\.?|pkt\.?|lit\.?|poz\.?|§)\s*", "", s)
    s = s.strip().rstrip(".)")
    return s if s else None


def build_unit_id(
    act_code: str,
    article: str,
    paragraph: Optional[Union[str, int]] = None,
    point: Optional[Union[str, int]] = None,
    letter: Optional[Union[str, int]] = None
) -> str:
    """
    Tworzy jednoznaczny, kanoniczny identyfikator jednostki prawnej w formacie kluczowanym:
    {act}:art={art}:par={par}:point={point}:letter={letter}
    Wartości brakujące kodowane są jako '-' (dywiz).
    Przykłady:
      - VAT:art=108a:par=1a:point=-:letter=-
      - UOR:art=2:par=1:point=2:letter=-
      - PIT:art=23:par=1:point=4:letter=-
      - PIT:art=27:par=1:point=-:letter=-
      - OP:art=193a:par=-:point=-:letter=-
      - VAT:art=108a:par=1:point=2:letter=a
    """
    act = normalize_act(act_code)
    art = normalize_article_token(article)
    par = normalize_sub_unit(paragraph) or "-"
    pt = normalize_sub_unit(point) or "-"
    let = normalize_sub_unit(letter) or "-"
    return f"{act}:art={art}:par={par}:point={pt}:letter={let}"


def parse_unit_id(unit_id: str) -> Dict[str, Optional[str]]:
    """
    Parsuje kanoniczny unit_id z powrotem do słownika z wartościami składowymi.
    """
    if not unit_id or ":" not in unit_id:
        return {"act": "", "article": "", "paragraph": None, "point": None, "letter": None}

    parts = unit_id.strip().split(":")
    act = parts[0]
    art = None
    par = None
    pt = None
    let = None

    for p in parts[1:]:
        if p.startswith("art="):
            art = p[4:]
        elif p.startswith("par="):
            v = p[4:]
            par = v if v != "-" else None
        elif p.startswith("point="):
            v = p[6:]
            pt = v if v != "-" else None
        elif p.startswith("letter="):
            v = p[7:]
            let = v if v != "-" else None

    return {
        "act": act,
        "article": art or "",
        "paragraph": par,
        "point": pt,
        "letter": let
    }
