import re
import os
from typing import List, Dict, Any, Optional
from bs4 import BeautifulSoup, Tag

ACT_CODE_MAP = {
    "PIT.html": ("PIT", "Ustawa o podatku dochodowym od osób fizycznych"),
    "CIT.html": ("CIT", "Ustawa o podatku dochodowym od osób prawnych"),
    "VAT.html": ("VAT", "Ustawa o podatku od towarów i usług"),
    "Ordynacja_Podatkowa.html": ("ORDYNACJA", "Ordynacja Podatkowa"),
    "UoR_Rachunkowosc.html": ("UOR", "Ustawa o rachunkowości"),
    "ZUS_System_Ubezpieczen.html": ("ZUS", "Ustawa o systemie ubezpieczeń społecznych"),
    "Prawo_Przedsiebiorcow.html": ("PP", "Ustawa - Prawo przedsiębiorców"),
}


def clean_html_text(text: str) -> str:
    """Oczyszcza tekst z twardych spacji, podwójnych odstępów i znaczników formatowania."""
    if not text:
        return ""
    text = text.replace('\xa0', ' ').replace('&nbsp;', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n\s*\n', '\n', text)
    return text.strip()


def parse_unit_dom_sota(unit: Tag, act_code: str, act_title: str, chapter_title: str, art_num: str) -> List[Dict[str, Any]]:
    """
    Parsowanie SOTA z wykorzystaniem struktury drzewiastej DOM (HTML Sejmu ISAP).
    Wyciąga pojedyncze punkty/ustępy z wstrzykiwaniem metadanych i zachowaniem Parent Context.
    """
    full_unit_text = clean_html_text(unit.get_text(separator="\n"))

    # Jeśli cały artykuł jest zwięzły (<= 5000 znaków), zwracamy go w całości z ustrukturyzowanymi metadanymi
    if len(full_unit_text) <= 5000:
        meta_content = (
            f"[AKTYWNE PRAWO]: {act_title}\n"
            f"[ROZDZIAŁ]: {chapter_title if chapter_title else 'Główny'}\n"
            f"[JEDNOSTKA]: {act_code} Art. {art_num}\n\n"
            f"{full_unit_text}"
        )
        return [{
            "act_code": act_code,
            "act_title": act_title,
            "chapter": chapter_title,
            "article_number": art_num,
            "full_title": f"{act_code} Art. {art_num}" + (f" ({chapter_title})" if chapter_title else ""),
            "content": meta_content,
            "clean_text": meta_content,
            "year_effective": 2026,
            "status": "OBOWIĄZUJĄCY"
        }]

    # DLA DŁUGICH ARTYKUŁÓW-KATALOGÓW (> 5000 znaków) STOSUJEMY PARSOWANIE DRZEWIASTE DOM SOTA:
    chunks = []

    # 1. Wyprowadzenie wpisu głównego (Parent Intro)
    intro_match = re.search(r'^(.*?)(?=\n\s*(?:1\)|pkt\s*1\)))', full_unit_text, re.DOTALL)
    if intro_match:
        intro_text = intro_match.group(1).strip()
    else:
        intro_text = full_unit_text[:250].strip()

    # 2. Wyciągnięcie ustępów doprecyzowujących (np. ust. 5a, 5b, 5c, 5e, 5f o samochodach)
    exec_sections = {}
    exec_matches = re.finditer(r'\n\s*(\d+[a-z]?)\.\n(.*?)(?=\n\s*\d+[a-z]?\.\n|\Z)', full_unit_text, re.DOTALL)
    for m in exec_matches:
        sec_num = m.group(1)
        sec_text = m.group(2).strip()
        exec_sections[sec_num] = f"ust. {sec_num}: {sec_text}"

    # 3. Dzielimy treść artykułu po poszczególnych punktach (np. 1), 2), 47a))
    point_blocks = re.split(r'\n(?=\s*\d+[a-z]?\))', full_unit_text)
    
    for block in point_blocks:
        block_text = block.strip()
        num_match = re.match(r'^\s*(\d+[a-z]?)\)', block_text)
        if not num_match:
            continue

        pkt_num = num_match.group(1)
        sub_art_num = f"{art_num} ust. 1 pkt {pkt_num}"

        # ODCIĘCIE OGONA: ścinamy dalsze ustępy (np. ust. 3b., ust. 4.), aby punkt zawierał WYŁĄCZNIE własną treść!
        clean_pkt_body = re.split(r'\n\s*\d+[a-z]?\.\n', block_text)[0].strip()

        # Doklejamy właściwe ustępy doprecyzowujące (np. ust. 5e o limicie 225 000 zł dla aut elektrycznych do pkt 47a)
        related_info = ""
        if pkt_num in {"4", "46", "46a", "47a"}:
            car_execs = [exec_sections[k] for k in ["5a", "5b", "5c", "5d", "5e", "5f", "5g", "5h"] if k in exec_sections]
            if car_execs:
                related_info = "\n\n[POWIĄZANE PREPISY DOPRECYZOWUJĄCE LIMIT I EKSPLOATACJĘ]:\n" + "\n".join(car_execs[:4])

        formatted_content = (
            f"[AKTYWNE PRAWO]: {act_title}\n"
            f"[ROZDZIAŁ]: {chapter_title if chapter_title else 'Główny'}\n"
            f"[JEDNOSTKA]: {act_code} Art. {sub_art_num}\n\n"
            f"Wprowadzenie: {intro_text}\n\n"
            f"Treść przepisu:\n{clean_pkt_body}"
            f"{related_info}"
        )

        chunks.append({
            "act_code": act_code,
            "act_title": act_title,
            "chapter": chapter_title,
            "article_number": sub_art_num,
            "full_title": f"{act_code} Art. {sub_art_num}" + (f" ({chapter_title})" if chapter_title else ""),
            "content": formatted_content,
            "clean_text": formatted_content,
            "year_effective": 2026,
            "status": "OBOWIĄZUJĄCY"
        })

    return chunks if chunks else [{
        "act_code": act_code,
        "act_title": act_title,
        "chapter": chapter_title,
        "article_number": art_num,
        "full_title": f"{act_code} Art. {art_num}" + (f" ({chapter_title})" if chapter_title else ""),
        "content": full_unit_text,
        "clean_text": full_unit_text,
        "year_effective": 2026,
        "status": "OBOWIĄZUJĄCY"
    }]


def extract_articles_from_html(file_path: str) -> List[Dict[str, Any]]:
    file_name = os.path.basename(file_path)
    act_code, act_title = ACT_CODE_MAP.get(file_name, (os.path.splitext(file_name)[0].upper(), f"Ustawa ({file_name})"))

    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        html_content = f.read()

    soup = BeautifulSoup(html_content, "html.parser")
    h1_tag = soup.find("h1")
    if h1_tag:
        extracted_title = clean_html_text(h1_tag.get_text())
        if len(extracted_title) > 5:
            act_title = extracted_title

    articles = []
    arti_units = soup.find_all("div", class_=lambda c: c and "unit_arti" in c)
    if not arti_units:
        arti_units = soup.find_all(["div", "section"], id=re.compile(r'arti_[\d\w]+'))

    for unit in arti_units:
        h3_art = unit.find("h3")
        art_num = ""
        if h3_art:
            art_num_text = clean_html_text(h3_art.get_text())
            match = re.search(r'Art\.\s*([\d\w]+)', art_num_text, re.IGNORECASE)
            if match:
                art_num = match.group(1)
            else:
                art_num = art_num_text.replace("Art.", "").strip().rstrip(".")

        chapter_title = ""
        parent_chpt = unit.find_parent("div", class_=lambda c: c and "unit_chpt" in c)
        if parent_chpt:
            chpt_h3 = parent_chpt.find("h3")
            if chpt_h3:
                chapter_title = clean_html_text(chpt_h3.get_text())

        raw_text = clean_html_text(unit.get_text(separator="\n"))
        if not art_num:
            match = re.search(r'Art\.\s*([\d\w]+)', raw_text[:50], re.IGNORECASE)
            if match:
                art_num = match.group(1)
            else:
                art_num = "N/A"

        # Uruchamiamy ustrukturyzowane parsowanie SOTA z Metadata Injection
        dom_chunks = parse_unit_dom_sota(unit, act_code, act_title, chapter_title, art_num)
        articles.extend(dom_chunks)

    unique_articles = {}
    for art in articles:
        key = (art["act_code"], art["article_number"])
        if key not in unique_articles or len(art["content"]) > len(unique_articles[key]["content"]):
            unique_articles[key] = art

    return list(unique_articles.values())
