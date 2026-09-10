import os
import sys
import json
import time
import re
import logging
from typing import List, Dict, Any, Union, Optional, Tuple, Sequence
from dataclasses import dataclass

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

# Konfiguracja logowania
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("eval_rag")

# Dodanie ścieżki katalogu głównego projektu oraz bezpiecznych katalogów cache
base_dir = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, base_dir)

cache_dir = os.path.join(base_dir, ".cache")
inductor_cache = os.path.join(cache_dir, "torch_inductor")
os.makedirs(inductor_cache, exist_ok=True)
os.environ["TORCHINDUCTOR_CACHE_DIR"] = inductor_cache
os.environ["TORCH_HOME"] = os.path.join(cache_dir, "torch")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from rag.retriever import retrieve_and_rerank
from rag.pipeline import run_rag_pipeline


# =====================================================================
# 1. MODEL CYTOWAŃ PRAWNYCH I NORMALIZACJA
# =====================================================================

def normalize_act(act_str: str) -> str:
    """
    Ujednolica kod aktu prawnego (np. OP, ORDYNACJA, ORDYNACJA_PODATKOWA -> OP,
    VAT, PIT, CIT, UOR, ZUS, PP itp.).
    """
    if not act_str:
        return ""
    s = act_str.strip().upper()
    if "ORDYNACJ" in s or s in ("OP", "ORD"):
        return "OP"
    if "VAT" in s:
        return "VAT"
    if "PIT" in s:
        return "PIT"
    if "CIT" in s:
        return "CIT"
    if "RACHUNKOWOŚC" in s or "RACHUNKOWOSC" in s or "UOR" in s:
        return "UOR"
    if "UBEZPIECZE" in s or "ZUS" in s:
        return "ZUS"
    if "PRZEDSIĘBIORC" in s or "PRZEDSIĘBIORCÓW" in s or "PRZEDSIĘBIORCY" in s or "PP" in s:
        return "PP"
    return re.sub(r"[^A-Z0-9_]", "", s)


def normalize_article_token(art_str: str) -> str:
    """
    Normalizuje numer i literę artykułu (np. 'Art. 28b' -> '28b', '23' -> '23', '2a' -> '2a').
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
    return s


def normalize_sub_unit(val: Optional[Union[str, int]]) -> Optional[str]:
    """Normalizuje numer ustępu, punktu lub litery (np. 'ust. 1' -> '1', '1a' -> '1a', 'pkt 4' -> '4')."""
    if val is None:
        return None
    s = str(val).strip().lower()
    s = re.sub(r"^(?:ust\.?|pkt\.?|lit\.?|poz\.?)\s*", "", s)
    s = s.strip()
    return s if s else None


@dataclass(frozen=True)
class LegalCitation:
    """
    Ścisła, niezmienna reprezentacja jednostki redakcyjnej przepisu prawa.
    Obsługuje typ str dla ustępu i punktu (np. 1a, 2b), zapewniając pełną zgodność
    z polską techniką prawodawczą.
    """
    act: str
    article: str
    paragraph: Optional[str] = None
    point: Optional[str] = None

    def __post_init__(self):
        object.__setattr__(self, "act", normalize_act(self.act))
        object.__setattr__(self, "article", normalize_article_token(self.article))
        object.__setattr__(self, "paragraph", normalize_sub_unit(self.paragraph))
        object.__setattr__(self, "point", normalize_sub_unit(self.point))

    def __str__(self) -> str:
        res = f"{self.act} Art. {self.article}"
        if self.paragraph:
            res += f" ust. {self.paragraph}"
        if self.point:
            res += f" pkt {self.point}"
        return res

    def to_dict(self) -> Dict[str, Any]:
        return {
            "act": self.act,
            "article": self.article,
            "paragraph": self.paragraph,
            "point": self.point
        }

    @classmethod
    def from_text(cls, text: str, default_act: Optional[str] = None) -> Optional["LegalCitation"]:
        """Parsuje tekst cytowania, np. 'PIT Art. 23 ust. 1 pkt 4', 'ORDYNACJA 193a'."""
        if not text:
            return None
        s = text.strip()
        act = default_act

        act_match = re.match(
            r"^(PIT|CIT|VAT|ORDYNACJA(?:\s+PODATKOWA)?|OP|UOR|ZUS|PP|Ustawa\s+o\s+[A-Za-zżźćńółęąśŻŹĆŃÓŁĘĄŚ]+)\b",
            s,
            re.IGNORECASE
        )
        if act_match:
            act = act_match.group(1)
            s = s[act_match.end():].strip()

        if not act:
            return None

        s = re.sub(r"^art\.?\s*", "", s, flags=re.IGNORECASE).strip()
        art_match = re.match(r"^(\d+)\s*([a-z])?(?=\s+(?:ust|pkt|lit|poz)\b|\b|$)", s, re.IGNORECASE)
        if not art_match:
            return None

        num = art_match.group(1)
        suffix = art_match.group(2) or ""
        article = f"{num}{suffix}".lower()
        remainder = s[art_match.end():].strip()

        paragraph = None
        point = None

        if remainder:
            ust_match = re.search(r"ust\.?\s*(\d+[a-z]?)", remainder, re.IGNORECASE)
            if ust_match:
                paragraph = ust_match.group(1)
            pkt_match = re.search(r"pkt\.?\s*(\d+[a-z]?)", remainder, re.IGNORECASE)
            if pkt_match:
                point = pkt_match.group(1)

        return cls(act=act, article=article, paragraph=paragraph, point=point)

    @classmethod
    def from_doc(cls, doc: Dict[str, Any]) -> Optional["LegalCitation"]:
        """Ekstrahuje LegalCitation ze struktury dokumentu z bazy/parsera."""
        act = doc.get("act_code") or doc.get("act") or doc.get("act_title")
        art_num = doc.get("article_number") or doc.get("article")
        if not act or not art_num:
            return None
        return cls.from_text(f"{act} {art_num}", default_act=str(act))


# Funkcje zachowujące kompatybilność wsteczną dla testów modułowych (np. test_ksef_and_decimal_math.py)
def normalize_article(art_str: str) -> Optional[Tuple[int, Optional[str]]]:
    """
    Zachowana dla kompatybilności wstecznej funkcja zwracająca (numer, sufix).
    """
    if not art_str:
        return None
    s = art_str.strip().lower()
    s = re.sub(r"^art\.?\s*", "", s)
    match = re.match(r"^(\d+)\s*([a-z])?(?:\s+ust|\s+pkt|\b|$)", s)
    if not match:
        return None
    num = int(match.group(1))
    suffix = match.group(2) if match.group(2) else None
    return num, suffix


def is_strict_citation_match(cand_act: str, cand_art: str, exp_act: str, exp_art: str) -> bool:
    """Zachowane dla kompatybilności wstecznej ścisłe dopasowanie artykułu i ustawy."""
    norm_cand_act = normalize_act(cand_act)
    norm_exp_act = normalize_act(exp_act)
    if norm_cand_act != norm_exp_act:
        return False
    c_norm = normalize_article(cand_art)
    e_norm = normalize_article(exp_art)
    if c_norm is None or e_norm is None:
        return False
    return c_norm == e_norm


def match_citation(candidate: LegalCitation, expected: LegalCitation, level: str = "article") -> bool:
    """
    Weryfikuje dopasowanie na 3 poziomach szczegółowości:
    - 'article': zgodność aktu i artykułu (np. PIT 23 == PIT 23)
    - 'paragraph': zgodność aktu, artykułu oraz ustępu (o ile ustęp został określony w expected)
    - 'exact': pełna zgodność aktu, artykułu, ustępu i punktu/litery
    """
    if candidate.act != expected.act or candidate.article != expected.article:
        return False
    if level == "article":
        return True
    if level == "paragraph":
        if expected.paragraph is not None:
            return candidate.paragraph == expected.paragraph
        return True
    if level == "exact":
        if expected.paragraph is not None and candidate.paragraph != expected.paragraph:
            return False
        if expected.point is not None and candidate.point != expected.point:
            return False
        return True
    raise ValueError(f"Nieznany poziom dopasowania: '{level}' (dozwolone: 'article', 'paragraph', 'exact')")


@dataclass(frozen=True)
class CitationGroup:
    """
    Grupa cytowań z jawną semantyką logiczną:
    - mode="any": alternatywa (spełniona, gdy odnaleziono co najmniej jedno cytowanie z grupy)
    - mode="all": koniunkcja (spełniona, gdy odnaleziono wszystkie wymagane cytowania z grupy)
    Całkowicie eliminuje błąd iloczynu kartezjańskiego (np. parowanie PIT z art. 16 z CIT).
    """
    mode: str
    citations: Tuple[LegalCitation, ...]

    def __init__(self, mode: str, citations: Sequence[Union[LegalCitation, Dict[str, Any], str]]):
        m = mode.lower().strip()
        if m not in ("any", "all"):
            raise ValueError(f"Tryb CitationGroup musi wynosić 'any' lub 'all', otrzymano: '{mode}'")
        object.__setattr__(self, "mode", m)
        parsed = []
        for c in citations:
            if isinstance(c, LegalCitation):
                parsed.append(c)
            elif isinstance(c, dict):
                parsed.append(LegalCitation(**c))
            elif isinstance(c, str):
                cit = LegalCitation.from_text(c)
                if cit:
                    parsed.append(cit)
        object.__setattr__(self, "citations", tuple(parsed))

    def evaluate(self, candidates: Sequence[LegalCitation], level: str = "article") -> Dict[str, Any]:
        matched = []
        unmatched = []
        for exp in self.citations:
            if any(match_citation(cand, exp, level=level) for cand in candidates):
                matched.append(exp)
            else:
                unmatched.append(exp)

        total = len(self.citations)
        if total == 0:
            return {"mode": self.mode, "satisfied": True, "recall": 1.0, "matched": [], "unmatched": []}

        if self.mode == "any":
            satisfied = len(matched) > 0
            recall = 1.0 if satisfied else 0.0
        else:  # all
            satisfied = len(matched) == total
            recall = len(matched) / total

        return {
            "mode": self.mode,
            "satisfied": satisfied,
            "recall": round(recall, 4),
            "matched": matched,
            "unmatched": unmatched
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "citations": [c.to_dict() for c in self.citations]
        }


def evaluate_question_citation_groups(
    candidates: Sequence[LegalCitation],
    groups: Sequence[CitationGroup],
    level: str = "article"
) -> Dict[str, Any]:
    """Oblicza complete-answer (1.0/0.0) oraz macro recall dla pojedynczego pytania."""
    if not groups:
        return {
            "complete_answer": 1.0,
            "macro_recall": 1.0,
            "group_evaluations": []
        }

    group_evals = [g.evaluate(candidates, level=level) for g in groups]
    all_satisfied = all(ge["satisfied"] for ge in group_evals)
    avg_recall = sum(ge["recall"] for ge in group_evals) / len(group_evals)

    return {
        "complete_answer": 1.0 if all_satisfied else 0.0,
        "macro_recall": round(avg_recall, 4),
        "group_evaluations": group_evals
    }


# =====================================================================
# 2. BENCHMARKOWY ZESTAW DANYCH (15 PYTAŃ Z PRAWA PODATKOWEGO)
# =====================================================================

TEST_DATASET: List[Dict[str, Any]] = [
    {
        "id": 1,
        "category": "PIT/CIT - Amortyzacja",
        "query": "Jaki jest limit wartości początkowej samochodów osobowych spalinowych dla odpisów amortyzacyjnych zaliczanych do KUP?",
        "expected_act": ["PIT", "CIT"],
        "expected_articles": ["23", "16"],
        "expected_citation_groups": [
            CitationGroup("any", [
                LegalCitation("PIT", "23", "1", "4"),
                LegalCitation("CIT", "16", "1", "4")
            ])
        ],
        "ground_truth_claims": ["150 000 zł", "amortyzacji", "samochód osobowy"]
    },
    {
        "id": 2,
        "category": "VAT - Pojazdy Mieszane",
        "query": "Ile wynosi odliczenie podatku naliczonego VAT od wydatków eksploatacyjnych i paliwa dla samochodu osobowego używanego w trybie mieszanym?",
        "expected_act": ["VAT"],
        "expected_articles": ["86a"],
        "expected_citation_groups": [
            CitationGroup("all", [
                LegalCitation("VAT", "86a", "1")
            ])
        ],
        "ground_truth_claims": ["50%", "użytkowanie mieszane", "podatek naliczony"]
    },
    {
        "id": 3,
        "category": "VAT - Split Payment",
        "query": "W jakich sytuacjach i dla jakich transakcji powyżej 15 tys. zł brutto występuje obowiązek stosowania Mechanizmu Podzielonej Płatności?",
        "expected_act": ["VAT"],
        "expected_articles": ["108a"],
        "expected_citation_groups": [
            CitationGroup("all", [
                LegalCitation("VAT", "108a", "1a")
            ])
        ],
        "ground_truth_claims": ["15 000 zł", "mechanizm podzielonej płatności", "split payment"]
    },
    {
        "id": 4,
        "category": "UoR - Księgi Rachunkowe",
        "query": "Przy jakim limicie przychodów netto ze sprzedaży powstaje obowiązek prowadzenia pełnych ksiąg rachunkowych wg Ustawy o Rachunkowości?",
        "expected_act": ["UOR", "PIT"],
        "expected_articles": ["2", "24a"],
        "expected_citation_groups": [
            CitationGroup("any", [
                LegalCitation("UOR", "2", "1", "2"),
                LegalCitation("PIT", "24a", "4")
            ])
        ],
        "ground_truth_claims": ["2 000 000 euro", "2 mln euro", "księgi rachunkowe"]
    },
    {
        "id": 5,
        "category": "PIT - Zaliczki",
        "query": "Do którego dnia miesiąca podatnik PIT prowadzący działalność ma obowiązek wpłacać miesięczną zaliczkę na podatek dochodowy?",
        "expected_act": ["PIT"],
        "expected_articles": ["44"],
        "expected_citation_groups": [
            CitationGroup("all", [
                LegalCitation("PIT", "44", "6")
            ])
        ],
        "ground_truth_claims": ["20 dnia", "zaliczka", "podatek dochodowy"]
    },
    {
        "id": 6,
        "category": "PIT/CIT - KUP",
        "query": "Jaka jest ogólna definicja kosztów uzyskania przychodów i jakie wymogi musi spełniać wydatek, aby stanowić KUP?",
        "expected_act": ["PIT", "CIT"],
        "expected_articles": ["22", "15"],
        "expected_citation_groups": [
            CitationGroup("any", [
                LegalCitation("PIT", "22", "1"),
                LegalCitation("CIT", "15", "1")
            ])
        ],
        "ground_truth_claims": ["w celu osiągnięcia przychodów", "zachowania albo zabezpieczenia źródła przychodów"]
    },
    {
        "id": 7,
        "category": "PIT/CIT - Ulga B+R",
        "query": "Na czym polega ulga podatkowa na działalność badawczo-rozwojową B+R i jakie koszty kwalifikowane można dodatkowo odliczyć?",
        "expected_act": ["PIT", "CIT"],
        "expected_articles": ["26e", "18d"],
        "expected_citation_groups": [
            CitationGroup("any", [
                LegalCitation("PIT", "26e"),
                LegalCitation("CIT", "18d")
            ])
        ],
        "ground_truth_claims": ["koszty kwalifikowane", "badawczo-rozwojowa", "odliczenie"]
    },
    {
        "id": 8,
        "category": "PIT - Skala Podatkowa",
        "query": "Jakie są stawki podatkowe w skali podatkowej PIT oraz kwota zmniejszająca podatek w pierwszym progu podatkowym?",
        "expected_act": ["PIT"],
        "expected_articles": ["27"],
        "expected_citation_groups": [
            CitationGroup("all", [
                LegalCitation("PIT", "27", "1")
            ])
        ],
        "ground_truth_claims": ["12%", "32%", "120 000 zł"]
    },
    {
        "id": 9,
        "category": "Ordynacja - JPK",
        "query": "Kiedy i w jakiej formie organ podatkowy może żądać przekazania ksiąg podatkowych w postaci Jednolitego Pliku Kontrolnego JPK?",
        "expected_act": ["ORDYNACJA", "VAT"],
        "expected_articles": ["193a", "99"],
        "expected_citation_groups": [
            CitationGroup("any", [
                LegalCitation("ORDYNACJA", "193a"),
                LegalCitation("VAT", "99")
            ])
        ],
        "ground_truth_claims": ["postać elektroniczna", "struktura logiczna", "JPK"]
    },
    {
        "id": 10,
        "category": "UoR - Rzetelny Obraz",
        "query": "Na czym polega nadrzędna zasada rzetelnego i jasnego obrazu sytuacji majątkowej i finansowej w Ustawie o Rachunkowości?",
        "expected_act": ["UOR"],
        "expected_articles": ["4"],
        "expected_citation_groups": [
            CitationGroup("all", [
                LegalCitation("UOR", "4", "1")
            ])
        ],
        "ground_truth_claims": ["rzetelnie i jasno", "sytuacja majątkowa", "wynik finansowy"]
    },
    {
        "id": 11,
        "category": "VAT - Kasy Rejestrujące",
        "query": "W jakich przypadkach podatnicy świadczący usługi lub sprzedający towary na rzecz osób fizycznych mają obowiązek stosowania kasy rejestrującej?",
        "expected_act": ["VAT"],
        "expected_articles": ["111"],
        "expected_citation_groups": [
            CitationGroup("all", [
                LegalCitation("VAT", "111", "1")
            ])
        ],
        "ground_truth_claims": ["kasa rejestrująca", "ewidencja obrotu", "osoby fizyczne"]
    },
    {
        "id": 12,
        "category": "PIT - Przychód z Działalności",
        "query": "Co zdaniem ustawy o PIT stanowi przychód z pozarolniczej działalności gospodarczej?",
        "expected_act": ["PIT"],
        "expected_articles": ["14"],
        "expected_citation_groups": [
            CitationGroup("all", [
                LegalCitation("PIT", "14", "1")
            ])
        ],
        "ground_truth_claims": ["kwoty należne", "przychód z działalności"]
    },
    {
        "id": 13,
        "category": "ZUS - Ubezpieczenia",
        "query": "Kto podlega obowiązkowo ubezpieczeniom emerytalnemu i rentowym z tytułu prowadzenia pozarolniczej działalności wg Ustawy o ZUS?",
        "expected_act": ["ZUS"],
        "expected_articles": ["6"],
        "expected_citation_groups": [
            CitationGroup("all", [
                LegalCitation("ZUS", "6", "1", "5")
            ])
        ],
        "ground_truth_claims": ["osoby fizyczne", "prowadzące działalność", "ubezpieczenia emerytalne i rentowe"]
    },
    {
        "id": 14,
        "category": "PP - Działalność Nierejestrowana",
        "query": "Jakie warunki przychodowe należy spełnić, aby prowadzić działalność nierejestrowaną zgodnie z Prawem Przedsiębiorców?",
        "expected_act": ["PP"],
        "expected_articles": ["5"],
        "expected_citation_groups": [
            CitationGroup("all", [
                LegalCitation("PP", "5", "1")
            ])
        ],
        "ground_truth_claims": ["działalność nieewidencjonowana", "minimalne wynagrodzenie", "działalność nierejestrowana"]
    },
    {
        "id": 15,
        "category": "VAT - WIS",
        "query": "Czym jest Wiążąca Informacja Stawkowa (WIS) i jaki organ ją wydaje dla potrzeb podatku VAT?",
        "expected_act": ["VAT"],
        "expected_articles": ["42a"],
        "expected_citation_groups": [
            CitationGroup("all", [
                LegalCitation("VAT", "42a")
            ])
        ],
        "ground_truth_claims": ["stawka podatku VAT", "klasyfikacja towaru lub usługi", "decyzja"]
    }
]


# =====================================================================
# 3. METRYKI EWALUACJI RETRIEVALU
# =====================================================================

def is_match(
    candidate: Dict[str, Any],
    expected_acts: Optional[Union[List[str], str]] = None,
    expected_arts: Optional[Union[List[str], str]] = None,
    groups: Optional[List[CitationGroup]] = None,
    level: str = "article"
) -> bool:
    """
    Sprawdza, czy odnaleziony artykuł odpowiada oczekiwanym cytowaniom.
    Wspiera grupy cytowań oraz zachowuje pełną kompatybilność wsteczną
    dla wywołań ze starymi parametrami (expected_acts, expected_arts).
    """
    cand_cit = LegalCitation.from_doc(candidate)
    if cand_cit is None:
        return False

    if groups:
        for grp in groups:
            for exp in grp.citations:
                if match_citation(cand_cit, exp, level=level):
                    return True
        return False

    acts = expected_acts if isinstance(expected_acts, list) else ([expected_acts] if expected_acts else [])
    arts = expected_arts if isinstance(expected_arts, list) else ([expected_arts] if expected_arts else [])
    cand_act = str(candidate.get("act_code", ""))
    cand_art = str(candidate.get("article_number", ""))
    for exp_act in acts:
        for exp_art in arts:
            if is_strict_citation_match(cand_act, cand_art, exp_act, exp_art):
                return True
    return False


def evaluate_retrieval_metrics(
    retrieved_docs: List[Dict[str, Any]],
    expected_acts: Optional[Union[List[str], str, List[CitationGroup]]] = None,
    expected_arts: Optional[Union[List[str], str]] = None,
    groups: Optional[List[CitationGroup]] = None
) -> Dict[str, Any]:
    """
    Oblicza metryki retrievalu:
    - Hit Rate@1, Hit Rate@3, Hit Rate@5 (na poziomie article)
    - Reciprocal Rank (RR) (na poziomie article)
    - Hit Rate@1, Hit Rate@3, Hit Rate@5 (na poziomie exact)
    - Reciprocal Rank (RR) (na poziomie exact)
    - complete_answer oraz macro_recall dla grup cytowań
    """
    if groups is None and isinstance(expected_acts, list) and len(expected_acts) > 0 and isinstance(expected_acts[0], CitationGroup):
        groups = expected_acts

    if groups is None:
        acts = expected_acts if isinstance(expected_acts, list) else ([expected_acts] if expected_acts else [])
        arts = expected_arts if isinstance(expected_arts, list) else ([expected_arts] if expected_arts else [])
        legacy_citations = [
            LegalCitation(act=act, article=art)
            for act in acts
            for art in arts
        ]
        groups = [CitationGroup(mode="any", citations=legacy_citations)] if legacy_citations else []

    hit_1 = 0
    hit_3 = 0
    hit_5 = 0
    rr = 0.0

    hit_1_exact = 0
    hit_3_exact = 0
    hit_5_exact = 0
    rr_exact = 0.0

    parsed_cands = []
    for rank, doc in enumerate(retrieved_docs, start=1):
        cand_cit = LegalCitation.from_doc(doc)
        if cand_cit is not None:
            parsed_cands.append(cand_cit)

        # Dopasowanie na poziomie article
        if is_match(doc, groups=groups, level="article"):
            if rr == 0.0:
                rr = 1.0 / rank
            if rank <= 1:
                hit_1 = 1
            if rank <= 3:
                hit_3 = 1
            if rank <= 5:
                hit_5 = 1

        # Dopasowanie na poziomie exact
        if is_match(doc, groups=groups, level="exact"):
            if rr_exact == 0.0:
                rr_exact = 1.0 / rank
            if rank <= 1:
                hit_1_exact = 1
            if rank <= 3:
                hit_3_exact = 1
            if rank <= 5:
                hit_5_exact = 1

    top5_cands = parsed_cands[:5]
    eval_article = evaluate_question_citation_groups(top5_cands, groups, level="article")
    eval_exact = evaluate_question_citation_groups(top5_cands, groups, level="exact")

    return {
        "hit_rate_1": hit_1,
        "hit_rate_3": hit_3,
        "hit_rate_5": hit_5,
        "reciprocal_rank": round(rr, 4),
        "hit_rate_1_exact": hit_1_exact,
        "hit_rate_3_exact": hit_3_exact,
        "hit_rate_5_exact": hit_5_exact,
        "reciprocal_rank_exact": round(rr_exact, 4),
        "complete_answer": eval_article["complete_answer"],
        "macro_recall": eval_article["macro_recall"],
        "complete_answer_exact": eval_exact["complete_answer"],
        "macro_recall_exact": eval_exact["macro_recall"],
    }


# =====================================================================
# 4. METRYKI EWALUACJI ODPOWIEDZI: PODOBIEŃSTWO LEKSYKALNE I UGRUNTOWANIE
# =====================================================================

def evaluate_lexical_similarity_heuristic(answer_text: str, ground_truth_claims: List[str]) -> float:
    """
    Heurystyka podobieństwa leksykalnego.
    Mierzy pokrycie kluczowych fraz wzorcowych w wygenerowanej odpowiedzi.
    """
    if not answer_text or "brak dopasowania" in answer_text.lower():
        return 0.0

    match_count = 0
    answer_lower = answer_text.lower()

    for claim in ground_truth_claims:
        claim_lower = claim.lower()
        if claim_lower in answer_lower or any(w in answer_lower for w in claim_lower.split() if len(w) > 3):
            match_count += 1

    score = match_count / len(ground_truth_claims) if ground_truth_claims else 1.0
    return round(score, 2)


def evaluate_claim_grounding(answer_text: str, context_text: str, ground_truth_claims: List[str]) -> float:
    """
    Weryfikacja ugruntowania odpowiedzi w kontekście:
    1. Sprawdza, czy kluczowe twierdzenia mają oparcie w przekazanym context_text.
    2. Sprawdza brak odwrócenia znaczenia (wykrywanie fałszywych negacji 'może' vs 'nie może').
    """
    if not answer_text or not context_text:
        return 0.0

    ans_lower = answer_text.lower()
    ctx_lower = context_text.lower()

    # Wykrywanie sprzeczności negacji:
    negation_words = ["nie może", "nie podlega", "nie przysługuje", "wyłączone z kosztów"]
    positive_words = ["może", "podlega", "przysługuje", "stanowi koszt"]

    for neg, pos in zip(negation_words, positive_words):
        if neg in ctx_lower and (pos in ans_lower and neg not in ans_lower):
            return 0.0
        if neg in ans_lower and (pos in ctx_lower and neg not in ctx_lower):
            return 0.0

    grounded_claims = 0
    for claim in ground_truth_claims:
        c_low = claim.lower()
        if c_low in ctx_lower and c_low in ans_lower:
            grounded_claims += 1
        elif any(w in ctx_lower and w in ans_lower for w in c_low.split() if len(w) > 4):
            grounded_claims += 1

    return round(grounded_claims / len(ground_truth_claims), 2) if ground_truth_claims else 1.0


def evaluate_faithfulness(answer_text: str, context_text: str, ground_truth_claims: List[str]) -> float:
    """
    Kompozytowa metryka wierności semantycznej:
    Łączy weryfikację ugruntowania w źródle (70%) z heurystyką frazową (30%).
    """
    grounding = evaluate_claim_grounding(answer_text, context_text, ground_truth_claims)
    lexical = evaluate_lexical_similarity_heuristic(answer_text, ground_truth_claims)
    return round(0.7 * grounding + 0.3 * lexical, 2)


# =====================================================================
# 5. GŁÓWNA PĘTLA EWALUACJI BENCHMARKU
# =====================================================================

def run_rag_evaluation(output_json_path: str = "eval_results.json") -> Dict[str, Any]:
    print("=" * 105)
    print(" 🧪 URUCHAMIANIE MODUŁU EWALUACJI RAG (BENCHMARK 15 PYTAŃ PODATKOWYCH)")
    print("=" * 105)

    start_time = time.time()
    results = []

    total_hit_1 = 0
    total_hit_3 = 0
    total_hit_5 = 0
    sum_mrr = 0.0

    total_hit_1_exact = 0
    total_hit_3_exact = 0
    total_hit_5_exact = 0
    sum_mrr_exact = 0.0

    sum_complete_answer = 0.0
    sum_macro_recall = 0.0
    sum_faithfulness = 0.0
    sum_grounding = 0.0
    gen_evaluated_count = 0

    act_data: Dict[str, List[Dict[str, Any]]] = {}

    print(f"{'ID':<3} | {'Kategoria / Temat':<24} | {'Ustawa':<9} | {'H@1':<5} | {'H@3':<5} | {'H@3(ex)':<7} | {'MRR':<6} | {'Compl':<5} | {'Faith':<10} | {'Status'}")
    print("-" * 108)

    for item in TEST_DATASET:
        item_id = item["id"]
        query = item["query"]
        expected_act = item["expected_act"] if isinstance(item["expected_act"], list) else [item["expected_act"]]
        expected_arts = item["expected_articles"]
        groups: List[CitationGroup] = item.get("expected_citation_groups", [])
        claims = item["ground_truth_claims"]

        # Wywołanie potoku RAG Pipeline (z mockiem na wypadek braku lokalnego llama.cpp)
        try:
            rag_output = run_rag_pipeline(user_query=query)
        except Exception as e:
            if "Gemma SLM Recognizer" in str(e) or "NewConnectionError" in str(e) or "ConnectionRefusedError" in str(e):
                from unittest.mock import patch, MagicMock
                with patch("requests.post") as mock_p:
                    mock_resp = MagicMock()
                    mock_resp.status_code = 200
                    mock_resp.json.return_value = {"choices": [{"message": {"content": "[]"}}]}
                    mock_p.return_value = mock_resp
                    rag_output = run_rag_pipeline(user_query=query)
            else:
                raise

        cited_docs = rag_output.get("cited_articles", [])
        answer_text = rag_output.get("answer_text", "")
        prompt_context = rag_output.get("prompt_to_copy", "")

        if not cited_docs:
            raw_docs = retrieve_and_rerank(query=query, top_k=5, score_threshold=0.0)
            cited_docs = raw_docs

        retrieval_metrics = evaluate_retrieval_metrics(cited_docs, groups=groups)

        # Status generacji odpowiedzi: not_run | completed | failed | partial | skipped
        gen_status = rag_output.get("generation_status")
        if not gen_status:
            if not answer_text or "Tryb testowy (bez API Gemini)" in answer_text:
                gen_status = "not_run"
            elif "BŁĄD" in answer_text.upper() and len(answer_text) < 120:
                gen_status = "failed"
            else:
                gen_status = "completed"

        grounding_score = evaluate_claim_grounding(answer_text, prompt_context, claims)
        lexical_score = evaluate_lexical_similarity_heuristic(answer_text, claims)

        if gen_status == "not_run":
            faith_score = None
            faith_disp = "not_run"
        else:
            faith_score = evaluate_faithfulness(answer_text, prompt_context, claims)
            sum_faithfulness += faith_score
            sum_grounding += grounding_score
            gen_evaluated_count += 1
            faith_disp = f"{faith_score:.2f}"

        h1 = retrieval_metrics["hit_rate_1"]
        h3 = retrieval_metrics["hit_rate_3"]
        h5 = retrieval_metrics["hit_rate_5"]
        mrr = retrieval_metrics["reciprocal_rank"]

        h1_ex = retrieval_metrics["hit_rate_1_exact"]
        h3_ex = retrieval_metrics["hit_rate_3_exact"]
        h5_ex = retrieval_metrics["hit_rate_5_exact"]
        mrr_ex = retrieval_metrics["reciprocal_rank_exact"]

        compl = retrieval_metrics["complete_answer"]
        recall = retrieval_metrics["macro_recall"]

        total_hit_1 += h1
        total_hit_3 += h3
        total_hit_5 += h5
        sum_mrr += mrr

        total_hit_1_exact += h1_ex
        total_hit_3_exact += h3_ex
        total_hit_5_exact += h5_ex
        sum_mrr_exact += mrr_ex

        sum_complete_answer += compl
        sum_macro_recall += recall

        # Zbieranie metryk per akt prawny
        question_acts = set(c.act for g in groups for c in g.citations)
        for act in question_acts:
            if act not in act_data:
                act_data[act] = []
            act_data[act].append({
                "hit_1": h1,
                "hit_3": h3,
                "hit_5": h5,
                "mrr": mrr,
                "complete_answer": compl,
                "macro_recall": recall
            })

        status_str = "✅ PASS" if h3 == 1 else "⚠️ FAIL"
        act_str = "/".join(expected_act)
        category_str = item["category"][:23]

        print(f"{item_id:<3} | {category_str:<24} | {act_str:<9} | {h1:<5} | {h3:<5} | {h3_ex:<7} | {mrr:<6.3f} | {int(compl):<5} | {faith_disp:<10} | {status_str}")

        results.append({
            "id": item_id,
            "category": item["category"],
            "query": query,
            "expected_act": expected_act,
            "expected_articles": expected_arts,
            "expected_citation_groups": [g.to_dict() for g in groups],
            "hit_rate_1": h1,
            "hit_rate_3": h3,
            "hit_rate_5": h5,
            "reciprocal_rank": mrr,
            "hit_rate_1_exact": h1_ex,
            "hit_rate_3_exact": h3_ex,
            "hit_rate_5_exact": h5_ex,
            "reciprocal_rank_exact": mrr_ex,
            "complete_answer": compl,
            "macro_recall": recall,
            "generation": {
                "status": gen_status,
                "faithfulness": faith_score if gen_status != "not_run" else None,
                "heuristic_grounding_score": grounding_score if gen_status != "not_run" else None,
                "lexical_similarity": lexical_score if gen_status != "not_run" else None,
            },
            "faithfulness": faith_score if gen_status != "not_run" else None,
            "top_retrieved_article": cited_docs[0].get("full_title") if cited_docs else None,
            "top_rerank_score": cited_docs[0].get("rerank_score") if cited_docs else 0.0
        })

    total_q = len(TEST_DATASET)
    avg_hit_1 = round(total_hit_1 / total_q, 4)
    avg_hit_3 = round(total_hit_3 / total_q, 4)
    avg_hit_5 = round(total_hit_5 / total_q, 4)
    avg_mrr = round(sum_mrr / total_q, 4)

    avg_hit_1_exact = round(total_hit_1_exact / total_q, 4)
    avg_hit_3_exact = round(total_hit_3_exact / total_q, 4)
    avg_hit_5_exact = round(total_hit_5_exact / total_q, 4)
    avg_mrr_exact = round(sum_mrr_exact / total_q, 4)

    complete_answer_rate = round(sum_complete_answer / total_q, 4)
    macro_citation_recall = round(sum_macro_recall / total_q, 4)
    avg_faithfulness = round(sum_faithfulness / gen_evaluated_count, 4) if gen_evaluated_count > 0 else None
    avg_grounding = round(sum_grounding / gen_evaluated_count, 4) if gen_evaluated_count > 0 else None
    elapsed_sec = round(time.time() - start_time, 2)

    # Obliczenie metryk per akt prawny
    metrics_by_act = {}
    for act, q_list in sorted(act_data.items()):
        act_cnt = len(q_list)
        metrics_by_act[act] = {
            "questions_count": act_cnt,
            "hit_rate_at_1": round(sum(x["hit_1"] for x in q_list) / act_cnt, 4),
            "hit_rate_at_3": round(sum(x["hit_3"] for x in q_list) / act_cnt, 4),
            "hit_rate_at_5": round(sum(x["hit_5"] for x in q_list) / act_cnt, 4),
            "mrr": round(sum(x["mrr"] for x in q_list) / act_cnt, 4),
            "complete_answer_rate": round(sum(x["complete_answer"] for x in q_list) / act_cnt, 4),
            "macro_recall": round(sum(x["macro_recall"] for x in q_list) / act_cnt, 4),
        }

    overall_gen_status = "completed" if gen_evaluated_count == total_q else ("partial" if gen_evaluated_count > 0 else "not_run")

    summary = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_questions": total_q,
        "elapsed_seconds": elapsed_sec,
        "metrics": {
            "hit_rate_at_1": avg_hit_1,
            "hit_rate_at_3": avg_hit_3,
            "hit_rate_at_5": avg_hit_5,
            "mrr": avg_mrr,
            "hit_rate_at_1_exact": avg_hit_1_exact,
            "hit_rate_at_3_exact": avg_hit_3_exact,
            "hit_rate_at_5_exact": avg_hit_5_exact,
            "mrr_exact": avg_mrr_exact,
            "macro_citation_recall": macro_citation_recall,
            "complete_answer_rate": complete_answer_rate,
            "generation": {
                "status": overall_gen_status,
                "evaluated_questions": gen_evaluated_count,
                "faithfulness": avg_faithfulness,
                "heuristic_grounding_score": avg_grounding
            },
            "faithfulness": avg_faithfulness
        },
        "metrics_by_act": metrics_by_act,
        "detailed_results": results
    }

    print("-" * 108)
    print(" 📊 ZBIORCZE PODSUMOWANIE METRYK RAG:")
    print(f"  • Hit Rate@1 (Artykuł):   {avg_hit_1 * 100:.1f}%")
    print(f"  • Hit Rate@3 (Artykuł):   {avg_hit_3 * 100:.1f}%")
    print(f"  • Hit Rate@5 (Artykuł):   {avg_hit_5 * 100:.1f}%")
    print(f"  • MRR (Artykuł):          {avg_mrr:.4f}")
    print(f"  • Hit Rate@1 (Exact):     {avg_hit_1_exact * 100:.1f}%")
    print(f"  • Hit Rate@3 (Exact):     {avg_hit_3_exact * 100:.1f}%")
    print(f"  • Hit Rate@5 (Exact):     {avg_hit_5_exact * 100:.1f}%")
    print(f"  • MRR (Exact):            {avg_mrr_exact:.4f}")
    print(f"  • Macro Citation Recall:  {macro_citation_recall * 100:.1f}%")
    print(f"  • Complete Answer Rate:   {complete_answer_rate * 100:.1f}%")
    if avg_faithfulness is not None:
        print(f"  • Faithfulness:           {avg_faithfulness * 100:.1f}% (status: {overall_gen_status})")
    else:
        print(f"  • Faithfulness:           brak (status: {overall_gen_status})")
    print(f"  • Czas wykonania:         {elapsed_sec} sek.")
    print("-" * 108)
    print(" 🏛️ WYNIKI PER AKT PRAWNY:")
    for act, m in metrics_by_act.items():
        print(f"  • {act:<6} (pytań: {m['questions_count']}) | H@3: {m['hit_rate_at_3']*100:.1f}% | Compl: {m['complete_answer_rate']*100:.1f}% | MRR: {m['mrr']:.3f}")
    print("=" * 108)

    with open(output_json_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"Zapisano pełny raport z ewaluacji do pliku: {os.path.abspath(output_json_path)}\n")
    return summary


if __name__ == "__main__":
    run_rag_evaluation()
