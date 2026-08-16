import os
import re
import json
import socket
import logging
import requests
from typing import Dict, Tuple, List, Any, Optional
from pypdf import PdfReader

from presidio_analyzer import AnalyzerEngine, PatternRecognizer, Pattern, EntityRecognizer, RecognizerResult
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig

logger = logging.getLogger(__name__)

def resolve_llama_url(base_url: Optional[str] = None) -> str:
    """Automatycznie dopasowuje adres serwera llama.cpp w zależności od środowiska (Docker vs Host Windows/Linux)."""
    raw_url = base_url or os.getenv("LOCAL_AI_URL", "http://127.0.0.1:8088/v1")
    clean_url = raw_url.rstrip("/")

    if "local-ai" in clean_url:
        try:
            socket.gethostbyname("local-ai")
            target_url = clean_url
        except Exception:
            target_url = clean_url.replace("local-ai:8080", "127.0.0.1:8088").replace("local-ai", "127.0.0.1:8088")
    else:
        target_url = clean_url

    if not target_url.endswith("/chat/completions") and not target_url.endswith("/completions"):
        target_url = f"{target_url}/chat/completions"

    return target_url

# Funkcja pomocnicza do tworzenia dedykowanego polskiego silnika NLP SpaCy pl_core_news_lg
def create_polish_nlp_engine():
    nlp_config = {
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": "pl", "model_name": "pl_core_news_lg"}]
    }
    provider = NlpEngineProvider(nlp_configuration=nlp_config)
    return provider.create_engine()



# =====================================================================
# 1. WALIDATORY MATEMATYCZNE (DETERMINISTYCZNE)
# =====================================================================

def validate_nip(nip_str: str) -> bool:
    if not nip_str or not isinstance(nip_str, str):
        return False
    digits = [int(d) for d in re.sub(r"\D", "", nip_str)]
    if len(digits) != 10:
        return False
    weights = [6, 5, 7, 2, 3, 4, 5, 6, 7]
    checksum = sum(d * w for d, w in zip(digits[:9], weights)) % 11
    return checksum != 10 and checksum == digits[9]

def validate_pesel(pesel_str: str) -> bool:
    if not pesel_str or not isinstance(pesel_str, str):
        return False
    digits = [int(d) for d in re.sub(r"\D", "", pesel_str)]
    if len(digits) != 11:
        return False
    weights = [1, 3, 7, 9, 1, 3, 7, 9, 1, 3]
    checksum = (10 - (sum(d * w for d, w in zip(digits[:10], weights)) % 10)) % 10
    return checksum == digits[10]

def validate_iban_pl(iban_str: str) -> bool:
    if not iban_str or not isinstance(iban_str, str):
        return False
    clean = re.sub(r"\s+", "", iban_str).upper()
    if clean.startswith("PL"):
        clean = clean[2:]
    if len(clean) != 26 or not clean.isdigit():
        return False
    numeric_iban = clean[2:] + "2521" + clean[:2]
    return int(numeric_iban) % 97 == 1

def validate_regon(regon_str: str) -> bool:
    if not regon_str or not isinstance(regon_str, str):
        return False
    digits = [int(d) for d in re.sub(r"\D", "", regon_str)]
    if len(digits) == 9:
        weights = [8, 9, 2, 3, 4, 5, 6, 7]
        checksum = sum(d * w for d, w in zip(digits[:8], weights)) % 11
        checksum = 0 if checksum == 10 else checksum
        return checksum == digits[8]
    elif len(digits) == 14:
        if not validate_regon("".join(map(str, digits[:9]))):
            return False
        weights = [2, 4, 8, 5, 0, 9, 7, 3, 6, 1, 2, 4, 8]
        checksum = sum(d * w for d, w in zip(digits[:13], weights)) % 11
        checksum = 0 if checksum == 10 else checksum
        return checksum == digits[13]
    return False

# =====================================================================
# 2. PATTERN RECOGNIZERS (Deterministyczne wzorce dla Presidio)
# =====================================================================

class CustomRegonRecognizer(PatternRecognizer):
    def __init__(self, **kwargs):
        patterns = [
            Pattern("REGON Label Pattern", r"(?i)\bREGON\s*:?\s*(\d{9}|\d{14})\b", 0.95),
            Pattern("PL REGON Pattern", r"\b(\d{9}|\d{14})\b", 0.85)
        ]
        kwargs.setdefault("supported_entity", "PL_REGON")
        super().__init__(patterns=patterns, **kwargs)

    def validate_result(self, pattern_text: str) -> bool:
        clean = re.sub(r"\D", "", pattern_text)
        return "REGON" in pattern_text.upper() or validate_regon(clean)

class CustomNipRecognizer(PatternRecognizer):
    def __init__(self, **kwargs):
        patterns = [
            Pattern("NIP Label Pattern", r"(?i)\bNIP\s*:?\s*(?:PL)?\s*(\d{3}[-\s]?\d{3}[-\s]?\d{2}[-\s]?\d{2}|\d{3}[-\s]?\d{2}[-\s]?\d{2}[-\s]?\d{3}|\d{10})\b", 0.95),
            Pattern("PL NIP Pattern", r"\b(?:\d{3}[-\s]?\d{3}[-\s]?\d{2}[-\s]?\d{2}|\d{3}[-\s]?\d{2}[-\s]?\d{2}[-\s]?\d{3}|\d{10})\b", 0.92)
        ]
        kwargs.setdefault("supported_entity", "PL_NIP")
        super().__init__(patterns=patterns, **kwargs)

    def validate_result(self, pattern_text: str) -> bool:
        clean = re.sub(r"\D", "", pattern_text)
        if len(clean) != 10:
            return False
        return "NIP" in pattern_text.upper() or validate_nip(clean)

class CustomVatIdRecognizer(PatternRecognizer):
    def __init__(self, **kwargs):
        patterns = [
            Pattern("EU VAT ID Pattern", r"\b(?:PL|NL|DE|FR|IT|ES|BE|AT|SE|DK|FI|CZ|SK|HU|RO|BG|HR|EE|LV|LT|IE|PT|GR|CY|MT|LU)\s*\d{8,12}\b", 0.92)
        ]
        kwargs.setdefault("supported_entity", "PL_NIP")
        super().__init__(patterns=patterns, **kwargs)

class CustomIbanRecognizer(PatternRecognizer):
    def __init__(self, **kwargs):
        patterns = [
            Pattern("PL IBAN Formatted Pattern", r"\b(?:PL)?\s*\d{2}(?:\s*\d{4}){6}\b", 0.95),
            Pattern("PL IBAN Pattern", r"\b(?:PL)?\s*[\d\s]{26,32}\b", 0.92)
        ]
        kwargs.setdefault("supported_entity", "PL_IBAN")
        super().__init__(patterns=patterns, **kwargs)

    def validate_result(self, pattern_text: str) -> bool:
        clean = re.sub(r"\D", "", pattern_text)
        return len(clean) == 26

class CustomPeselRecognizer(PatternRecognizer):
    def __init__(self, **kwargs):
        patterns = [Pattern("PL PESEL Pattern", r"\b\d{11}\b", 0.95)]
        kwargs.setdefault("supported_entity", "PL_PESEL")
        super().__init__(patterns=patterns, **kwargs)

    def validate_result(self, pattern_text: str) -> bool:
        return validate_pesel(pattern_text)

class CustomEmailRecognizer(PatternRecognizer):
    def __init__(self, **kwargs):
        patterns = [Pattern("Email Pattern", r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", 0.99)]
        kwargs.setdefault("supported_entity", "EMAIL_ADDRESS")
        super().__init__(patterns=patterns, **kwargs)

class CustomPhoneRecognizer(PatternRecognizer):
    def __init__(self, **kwargs):
        # Wymaga jawnego formatu ze spacjami/myślnikami lub prefiksu międzynarodowego (+48), aby nie tokenizować dat ani numerów rejestrowych
        patterns = [
            Pattern("Grouped Phone Pattern", r"\b(?:\+\d{1,3}[\s-]?)?\(?\d{2,4}\)?[\s-]\d{3}[\s-]\d{3,4}\b", 0.95),
            Pattern("Intl Prefixed Phone Pattern", r"\b\+\d{10,13}\b", 0.95)
        ]
        kwargs.setdefault("supported_entity", "PHONE_NUMBER")
        super().__init__(patterns=patterns, **kwargs)

class CustomCompanyRecognizer(PatternRecognizer):
    def __init__(self, **kwargs):
        patterns = [
            Pattern(
                "PL/EU Company Pattern",
                r"(?<![A-Za-z0-9ĄĆĘŁŃÓŚŹŻąćęłńóśźż])(?:[A-Za-z0-9ĄĆĘŁŃÓŚŹŻąćęłńóśźż&\.\-]+\s+){1,6}(?:Sp\.\s*z\s*o\.o\.|S\.A\.|Sp\.\s*k\.|Sp\.\s*j\.|Sp\.\s*p\.|B\.V\.|GmbH|LLC|INC\.|Spółka\s+z\s+o\.o\.|Spółka\s+Komandytowa|Spółka\s+Akcyjna|Spółka\s+Jawna)(?![A-Za-z0-9ĄĆĘŁŃÓŚŹŻąćęłńóśźż])",
                0.95
            )
        ]
        kwargs.setdefault("supported_entity", "COMPANY_NAME")
        super().__init__(patterns=patterns, **kwargs)

class CustomKsefIdRecognizer(PatternRecognizer):
    def __init__(self, **kwargs):
        patterns = [
            Pattern("KSeF Number Pattern", r"\b\d{10}-\d{8}-[A-Za-z0-9]{12}-\d{2}\b", 0.99)
        ]
        kwargs.setdefault("supported_entity", "KSEF_ID")
        super().__init__(patterns=patterns, **kwargs)

# =====================================================================
# 3. SLM RECOGNIZER (Gemma4-e4b z llama.cpp dla PII z kontekstu)
# =====================================================================

class GemmaSlmRecognizer(EntityRecognizer):
    """Odpytuje lokalną Gemma 4 w llama.cpp o kontekstowe PII (Nazwy firm, Adresy, Imiona)."""
    def __init__(self, llama_cpp_url: Optional[str] = None, **kwargs):
        kwargs.setdefault("supported_entities", ["COMPANY_NAME", "ADDRESS", "PERSON"])
        kwargs.setdefault("name", "Gemma SLM Recognizer")
        super().__init__(**kwargs)
        
        self.llama_cpp_url = resolve_llama_url(llama_cpp_url)





    def analyze(self, text: str, entities: List[str], nlp_artifacts=None) -> List[RecognizerResult]:
        if not text or not text.strip():
            return []

        results = []
        prompt = f"""
        Jesteś audytorem RODO i ekspertem ds. anonimizacji faktur. Przeanalizuj poniższy tekst faktury i wypisz wyłącznie zidentyfikowane dane osobowe i adresowe:

        KATEGORIE DANYCH DO ANONIMIZACJI:
        1. COMPANY_NAME: Nazwy firm, spółek, dostawców i odbiorców (np. "Tech-Distrib EU B.V.", "INNOVATECH SP. Z O.O.")
        2. ADDRESS: Pełne adresy, ulice, kody pocztowe, miejscowości i kraje (np. "Keizersgracht 421", "Marszałkowska 126 / 134", "00-008 Warszawa, Holandia")
        3. PERSON: Imiona i nazwiska konkretnych osób fizycznych (np. "Jan Kowalski", "Anna Nowak")

        KRYTYCZNE ZASADY KONTROLNE (NEGATYWNE):
        - BEZWZGLĘDNIE NIE anonimizuj nagłówków ani etykiet faktury (np. SELLER, BUYER, IMPORTER, WDT, WNT, VAT, Opłacono, Ref, KvK, NIP, REGON, Data, Kwota, Razem, Faktura).
        - BEZWZGLĘDNIE NIE oznaczaj słów SELLER, BUYER, WDT, TAX ani słów prawnych jako PERSON!
        - Jeśli fraza to nazwa firmy (np. "Tech-Distrib EU B.V."), sklasyfikuj ją jako COMPANY_NAME, a NIE PERSON.

        Zwróć WYŁĄCZNIE poprawny JSON w formacie:
        [
          {{"entity_type": "COMPANY_NAME", "text": "Tech-Distrib EU B.V."}},
          {{"entity_type": "ADDRESS", "text": "Keizersgracht 421"}},
          {{"entity_type": "PERSON", "text": "Jan Kowalski"}}
        ]

        Tekst faktury:
        "{text}"
        """
        try:
            resp = requests.post(
                self.llama_cpp_url,
                json={
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.0,
                    "response_format": {"type": "json_object"}
                },
                timeout=15
            )
            if resp.status_code != 200:
                raise RuntimeError(f"Lokalny serwer LLM zwrócił status HTTP {resp.status_code}: {resp.text[:300]}")

            data = resp.json()
            if "choices" not in data or not data["choices"]:
                raise RuntimeError(f"Lokalny serwer LLM nie zwrócił pola 'choices' w odpowiedzi JSON: {data}")

            raw_content = data["choices"][0]["message"]["content"]
            parsed_items = json.loads(raw_content)


            if isinstance(parsed_items, dict):
                if "items" in parsed_items and isinstance(parsed_items["items"], list):
                    parsed_items = parsed_items["items"]
                elif "data" in parsed_items and isinstance(parsed_items["data"], list):
                    parsed_items = parsed_items["data"]
                elif parsed_items.values():
                    first_val = list(parsed_items.values())[0]
                    parsed_items = first_val if isinstance(first_val, list) else []
                else:
                    parsed_items = []


            if isinstance(parsed_items, list):
                for item in parsed_items:
                    if not isinstance(item, dict):
                        continue
                    phrase = str(item.get("text", "")).strip()
                    entity_type = item.get("entity_type")
                    if not phrase or len(phrase) < 2 or entity_type not in self.supported_entities:
                        continue

                    # Ignorujemy nagłówki faktury, gdyby model mimo wszystko je zwrócił
                    if phrase.upper() in {"SELLER", "BUYER", "IMPORTER", "WDT", "WNT", "VAT", "OPŁACONO", "PAID", "TAX", "REF"}:
                        continue

                    for match in re.finditer(re.escape(phrase), text, re.IGNORECASE):
                        results.append(
                            RecognizerResult(
                                entity_type=entity_type,
                                start=match.start(),
                                end=match.end(),
                                score=0.85
                            )
                        )
        except Exception as e:
            logger.error(f"[Presidio] Gemma SLM Recognizer (port 8080) jest niedostępna: {e}")
            raise RuntimeError(f"Gemma SLM Recognizer (port 8080) jest niedostępna lub zwróciła błąd: {e}") from e

        return results





# =====================================================================
# 4. ORKIESTRATOR PRESIDIO (Krok A)
# =====================================================================

class PresidioInvoiceSanitizer:
    def __init__(self, llama_cpp_url: Optional[str] = None):

        try:
            nlp_engine = create_polish_nlp_engine()
            self.analyzer = AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=["pl"])
            logger.info("Pomyślnie załadowano polski model językowy NLP: pl_core_news_lg dla Presidio.")
        except Exception as e:
            logger.warning(f"Nie udało się załadować dedykowanego polskiego silnika pl_core_news_lg ({e}). Używam domyślnego.")
            self.analyzer = AnalyzerEngine()

        self.anonymizer = AnonymizerEngine()

        # Rejestracja własnych rozpoznawaczy deterministycznych oraz SLM (z obsługą języka polskiego pl)
        self.analyzer.registry.add_recognizer(CustomRegonRecognizer(supported_language="pl"))
        self.analyzer.registry.add_recognizer(CustomNipRecognizer(supported_language="pl"))
        self.analyzer.registry.add_recognizer(CustomVatIdRecognizer(supported_language="pl"))
        self.analyzer.registry.add_recognizer(CustomIbanRecognizer(supported_language="pl"))
        self.analyzer.registry.add_recognizer(CustomPeselRecognizer(supported_language="pl"))
        self.analyzer.registry.add_recognizer(CustomEmailRecognizer(supported_language="pl"))
        self.analyzer.registry.add_recognizer(CustomPhoneRecognizer(supported_language="pl"))
        self.analyzer.registry.add_recognizer(CustomCompanyRecognizer(supported_language="pl"))
        self.analyzer.registry.add_recognizer(CustomKsefIdRecognizer(supported_language="pl"))
        self.analyzer.registry.add_recognizer(GemmaSlmRecognizer(llama_cpp_url=llama_cpp_url, supported_language="pl"))

    def sanitize(self, raw_text: str) -> Tuple[str, Dict[str, str]]:
        if not raw_text or not raw_text.strip():
            return "", {}

        analysis_results = self.analyzer.analyze(
            text=raw_text,
            entities=["PL_NIP", "PL_REGON", "PL_IBAN", "PL_PESEL", "COMPANY_NAME", "ADDRESS", "PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "KSEF_ID", "orgName", "persName"],
            language="pl"
        )

        # Normalizujemy specyficzne encje SpaCy PL (orgName -> COMPANY_NAME, persName -> PERSON)
        for res in analysis_results:
            if res.entity_type == "orgName":
                res.entity_type = "COMPANY_NAME"
            elif res.entity_type == "persName":
                res.entity_type = "PERSON"

        # Odfiltrowujemy fałszywie dodatnie wyniki domyślnego angielskiego rozpoznawacza PERSON i PHONE_NUMBER w SpaCy,
        # zachowując wyłącznie zweryfikowane imiona/nazwiska wykryte przez Gemma SLM (score >= 0.85) oraz własne wysokie wskaźniki dla telefonów (score >= 0.90).
        filtered_results = [
            res for res in analysis_results 
            if (res.entity_type != "PERSON" or res.score >= 0.85) and (res.entity_type != "PHONE_NUMBER" or res.score >= 0.90)
        ]

        # Usuwamy nakładające się na siebie przedziały (overlapping spans), dając pierwszeństwo dopasowaniom o wyższym score
        filtered_results = sorted(filtered_results, key=lambda res: res.score, reverse=True)
        clean_results = []
        for res in filtered_results:
            overlap = False
            for keep in clean_results:
                if max(res.start, keep.start) < min(res.end, keep.end):
                    overlap = True
                    break
            if not overlap:
                clean_results.append(res)

        analysis_results = sorted(clean_results, key=lambda res: res.start)

        mapping_dict: Dict[str, str] = {}
        entity_counters: Dict[str, int] = {}

        def presidio_token_operator(original_val: str, entity_type: str) -> str:
            if entity_type not in entity_counters:
                entity_counters[entity_type] = 1
            else:
                entity_counters[entity_type] += 1

            token = f"<{entity_type}_{entity_counters[entity_type]}>"
            mapping_dict[token] = original_val
            return token

        unique_entities = set(res.entity_type for res in analysis_results)
        operators = {}
        for ent_type in unique_entities:
            operators[ent_type] = OperatorConfig(
                "custom",
                {"lambda": lambda val, ent=ent_type: presidio_token_operator(val, ent)}
            )

        anonymized_response = self.anonymizer.anonymize(
            text=raw_text,
            analyzer_results=analysis_results,
            operators=operators
        )

        return anonymized_response.text, mapping_dict

# =====================================================================
# 5. EKSTRAKCJA TEKSTU Z PDF
# =====================================================================

def extract_pdf_text(file_path: str) -> str:
    if not file_path or not os.path.exists(file_path):
        return ""
    try:
        reader = PdfReader(file_path)
        text_parts = []
        for page in reader.pages:
            t = page.extract_text()
            if t:
                text_parts.append(t)
        return "\n".join(text_parts).strip()
    except Exception as e:
        logger.warning(f"Failed to extract PDF text from {file_path}: {e}")
        return ""

# =====================================================================
# 5B. EKSTRAKCJA KSEF XML (Krajowy System e-Faktur)
# =====================================================================

def extract_ksef_xml_text(file_path: str) -> str:
    """Odczytuje i parsuje plik faktury KSeF w formacie XML (FA_VAT / FA_2)."""
    if not file_path or not os.path.exists(file_path):
        return ""
    try:
        import xml.etree.ElementTree as ET
        tree = ET.parse(file_path)
        root = tree.getroot()
        xml_str = ET.tostring(root, encoding="utf-8").decode("utf-8")
        return xml_str.strip()
    except Exception as e:
        logger.warning(f"Failed to parse KSeF XML from {file_path}: {e}")
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read().strip()
        except Exception:
            return ""

# =====================================================================
# 6. TOLERANCYJNY DETOKENIZATOR (Ścisłe Granice & Inspection)
# =====================================================================

def tolerant_detokenize(cloud_json: Dict[str, Any], mapping_dict: Dict[str, str]) -> Tuple[Dict[str, Any], bool]:
    if not cloud_json or not isinstance(cloud_json, dict):
        return cloud_json or {}, True

    json_str = json.dumps(cloud_json, ensure_ascii=False)

    if mapping_dict and isinstance(mapping_dict, dict):
        for token, original_val in mapping_dict.items():
            core_token = str(token).strip("<>[]")
            parts = re.split(r'[_\s]+', core_token)

            regex_pattern = r'(?<![A-Za-z0-9_])[<\[]\s*' + r'[_\s]+'.join([re.escape(p) for p in parts]) + r'\s*[>\]]:?'

            escaped_val = json.dumps(str(original_val))[1:-1]
            json_str = re.sub(regex_pattern, lambda m, val=escaped_val: val, json_str, flags=re.IGNORECASE)

    leftover_pattern = r'[<\[]\s*[A-Z0-9_\s]{3,}\s*[>\]]'
    has_leftover_tokens = bool(re.search(leftover_pattern, json_str))

    if has_leftover_tokens:
        leftovers = re.findall(leftover_pattern, json_str)
        logger.warning(f"Detokenizer: Found unreplaced leftover tokens in output JSON: {leftovers}")

    try:
        detokenized_dict = json.loads(json_str)
    except json.JSONDecodeError as err:
        logger.error(f"Detokenizer: Failed to parse json after replacement: {err}")
        return cloud_json, True

    return detokenized_dict, has_leftover_tokens

def detokenize_text(text: str, mapping_dict: Dict[str, str]) -> Tuple[str, bool]:
    """Zastępuje wszystkie tokeny <TOKEN_X> w zwykłym tekście/markdown wartościami ze słownika RODO."""
    if not text:
        return "", False

    result_text = text

    if mapping_dict and isinstance(mapping_dict, dict):
        for token, original_val in mapping_dict.items():
            core_token = str(token).strip("<>[]")
            parts = re.split(r'[_\s]+', core_token)

            regex_pattern = r'(?<![A-Za-z0-9_])[<\[]\s*' + r'[_\s]+'.join([re.escape(p) for p in parts]) + r'\s*[>\]]:?'
            escaped_val = str(original_val)
            result_text = re.sub(regex_pattern, lambda m, val=escaped_val: val, result_text, flags=re.IGNORECASE)

    leftover_pattern = r'[<\[]\s*[A-Z0-9_\s]{3,}\s*[>\]]'
    has_leftover_tokens = bool(re.search(leftover_pattern, result_text))

    return result_text, has_leftover_tokens
