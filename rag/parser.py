import os
import re
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from bs4 import BeautifulSoup, Tag
from rag.units import normalize_act, normalize_article_token, normalize_sub_unit, build_unit_id

ACT_CODE_MAP = {
    "PIT.html": ("PIT", "Ustawa o podatku dochodowym od osób fizycznych"),
    "CIT.html": ("CIT", "Ustawa o podatku dochodowym od osób prawnych"),
    "VAT.html": ("VAT", "Ustawa o podatku od towarów i usług"),
    "Ordynacja_Podatkowa.html": ("OP", "Ordynacja podatkowa"),
    "UoR_Rachunkowosc.html": ("UOR", "Ustawa o rachunkowości"),
    "ZUS_System_Ubezpieczen.html": ("ZUS", "Ustawa o systemie ubezpieczeń społecznych"),
    "Prawo_Przedsiebiorcow.html": ("PP", "Ustawa - Prawo przedsiębiorców"),
}


@dataclass
class LegalUnit:
    act_code: str
    article: str
    paragraph: Optional[str] = None
    point: Optional[str] = None
    letter: Optional[str] = None
    unit_type: str = "article"  # 'article', 'paragraph', 'point', 'letter'
    unit_id: str = ""
    parent_unit_id: Optional[str] = None
    text: str = ""
    parent_intro: str = ""
    chapter: str = ""
    act_title: str = ""
    children: List["LegalUnit"] = field(default_factory=list)


def clean_html_text(text: str) -> str:
    """Oczyszcza tekst z twardych spacji, podwójnych odstępów i znaczników formatowania."""
    if not text:
        return ""
    text = text.replace('\xa0', ' ').replace('&nbsp;', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n\s*\n', '\n', text)
    return text.strip()


def remove_editorial_noise(tag: Tag):
    """Usuwa przypisy i odnośniki redakcyjne (unit_odno, pro-comm, odno_XXX)."""
    for noise in tag.find_all(class_=lambda c: c and any(x in c for x in ['unit_odno', 'pro-comm', 'pro-none'])):
        if noise.name not in ['h3', 'h4', 'h5']:
            noise.decompose()
    for noise in tag.find_all(id=re.compile(r'odno_\d+')):
        noise.decompose()


def extract_article_number_from_unit(unit: Tag) -> str:
    """Ekstrahuje czysty numer artykułu z nagłówka h3 lub id."""
    h3_art = unit.find("h3")
    if h3_art:
        txt = clean_html_text(h3_art.get_text())
        m = re.search(r'Art\.\s*([\d\w]+)', txt, re.IGNORECASE)
        if m:
            return normalize_article_token(m.group(1))
    uid = unit.get("id", "")
    m = re.search(r'arti_([\d\w]+)', uid, re.IGNORECASE)
    if m:
        return normalize_article_token(m.group(1))
    return ""


def extract_legal_tree(html_content: str, act_code: str, act_title: str) -> List[LegalUnit]:
    """
    Poziom 1: Ekstrakcja drzewa LegalUnit z DOM Sejmu ISAP.
    Obsługuje unit_arti -> unit_pass / unit_para -> unit_pint -> unit_lett.
    """
    soup = BeautifulSoup(html_content, "html.parser")
    articles: List[LegalUnit] = []

    arti_units = soup.find_all("div", class_=lambda c: c and "unit_arti" in c)
    if not arti_units:
        arti_units = soup.find_all(["div", "section"], id=re.compile(r'arti_[\d\w]+'))

    for unit in arti_units:
        art_num = extract_article_number_from_unit(unit)
        if not art_num:
            continue

        chapter_title = ""
        parent_chpt = unit.find_parent("div", class_=lambda c: c and "unit_chpt" in c)
        if parent_chpt:
            chpt_h3 = parent_chpt.find("h3")
            if chpt_h3:
                chapter_title = clean_html_text(chpt_h3.get_text())

        art_unit_id = build_unit_id(act_code, art_num)
        art_node = LegalUnit(
            act_code=act_code,
            article=art_num,
            unit_type="article",
            unit_id=art_unit_id,
            chapter=chapter_title,
            act_title=act_title
        )

        pass_units = unit.find_all("div", class_=lambda c: c and ("unit_pass" in c or "unit_para" in c))

        if not pass_units:
            remove_editorial_noise(unit)
            art_text = clean_html_text(unit.get_text(separator="\n"))
            art_node.text = art_text
            articles.append(art_node)
            continue

        first_pass = pass_units[0]
        intro_parts = []
        for prev in first_pass.find_previous_siblings():
            if prev.name in ['p', 'span', 'div'] and 'unit' not in prev.get('class', []):
                t = clean_html_text(prev.get_text())
                if t and not t.startswith("Art."):
                    intro_parts.append(t)
        art_node.parent_intro = " ".join(reversed(intro_parts))

        for pass_div in pass_units:
            pass_id = pass_div.get("id", "")
            m_pass = re.search(r'(?:pass|para)_([\d\w]+)', pass_id)
            pass_num = normalize_sub_unit(m_pass.group(1)) if m_pass else None

            if not pass_num:
                pass_h = pass_div.find(["h4", "h5", "span", "p"])
                if pass_h:
                    m = re.match(r'^(?:§\s*)?(\d+[a-z]?)\.', clean_html_text(pass_h.get_text()))
                    if m:
                        pass_num = normalize_sub_unit(m.group(1))

            pass_unit_id = build_unit_id(act_code, art_num, paragraph=pass_num)
            pass_node = LegalUnit(
                act_code=act_code,
                article=art_num,
                paragraph=pass_num,
                unit_type="paragraph",
                unit_id=pass_unit_id,
                parent_unit_id=art_unit_id,
                parent_intro=art_node.parent_intro,
                chapter=chapter_title,
                act_title=act_title
            )

            pint_units = pass_div.find_all("div", class_=lambda c: c and "unit_pint" in c)

            if not pint_units:
                remove_editorial_noise(pass_div)
                pass_node.text = clean_html_text(pass_div.get_text(separator="\n"))
                art_node.children.append(pass_node)
                continue

            first_pint = pint_units[0]
            pass_intro_parts = []
            for prev in first_pint.find_previous_siblings():
                t = clean_html_text(prev.get_text())
                if t:
                    pass_intro_parts.append(t)
            pass_node.parent_intro = " ".join(reversed(pass_intro_parts))
            pass_node.text = pass_node.parent_intro

            for pint_div in pint_units:
                pint_id = pint_div.get("id", "")
                m_pint = re.search(r'pint_([\d\w]+)', pint_id)
                pt_num = normalize_sub_unit(m_pint.group(1)) if m_pint else None
                if not pt_num:
                    pt_h = pint_div.find(["span", "p", "div"])
                    if pt_h:
                        m = re.match(r'^(\d+[a-z]?)\)', clean_html_text(pt_h.get_text()))
                        if m:
                            pt_num = normalize_sub_unit(m.group(1))

                if not pt_num or "odno" in pint_id:
                    continue

                pint_unit_id = build_unit_id(act_code, art_num, paragraph=pass_num, point=pt_num)
                pint_node = LegalUnit(
                    act_code=act_code,
                    article=art_num,
                    paragraph=pass_num,
                    point=pt_num,
                    unit_type="point",
                    unit_id=pint_unit_id,
                    parent_unit_id=pass_unit_id,
                    parent_intro=f"{art_node.parent_intro}\n{pass_node.parent_intro}".strip(),
                    chapter=chapter_title,
                    act_title=act_title
                )

                lett_units = pint_div.find_all("div", class_=lambda c: c and "unit_lett" in c)
                if not lett_units:
                    remove_editorial_noise(pint_div)
                    pint_node.text = clean_html_text(pint_div.get_text(separator="\n"))
                    pass_node.children.append(pint_node)
                else:
                    first_lett = lett_units[0]
                    lett_intro_parts = []
                    for prev in first_lett.find_previous_siblings():
                        t = clean_html_text(prev.get_text())
                        if t:
                            lett_intro_parts.append(t)
                    pint_node.text = " ".join(reversed(lett_intro_parts))

                    for lett_div in lett_units:
                        lett_id = lett_div.get("id", "")
                        m_let = re.search(r'lett_([\d\w]+)', lett_id)
                        let_token = normalize_sub_unit(m_let.group(1)) if m_let else None
                        if not let_token or "odno" in lett_id:
                            continue

                        lett_unit_id = build_unit_id(act_code, art_num, paragraph=pass_num, point=pt_num, letter=let_token)
                        remove_editorial_noise(lett_div)
                        lett_text = clean_html_text(lett_div.get_text(separator="\n"))
                        lett_node = LegalUnit(
                            act_code=act_code,
                            article=art_num,
                            paragraph=pass_num,
                            point=pt_num,
                            letter=let_token,
                            unit_type="letter",
                            unit_id=lett_unit_id,
                            parent_unit_id=pint_unit_id,
                            parent_intro=f"{pint_node.parent_intro}\n{pint_node.text}".strip(),
                            text=lett_text,
                            chapter=chapter_title,
                            act_title=act_title
                        )
                        pint_node.children.append(lett_node)

                    pass_node.children.append(pint_node)

            art_node.children.append(pass_node)

        articles.append(art_node)

    return articles


def format_chunk_title(act_code: str, art_num: str, par: Optional[str] = None, pt: Optional[str] = None, let: Optional[str] = None) -> str:
    """Formatuje czytelny tytuł jednostki prawnej."""
    title = f"{act_code} Art. {art_num}"
    if par:
        title += f" ust. {par}"
    if pt:
        title += f" pkt {pt}"
    if let:
        title += f" lit. {let}"
    return title


def build_retrieval_chunks(tree: List[LegalUnit]) -> List[Dict[str, Any]]:
    """
    Poziom 2: Przekształca drzewo LegalUnit na płaską listę fragmentów do indeksu PostgreSQL / wektora.
    Wstrzykuje metadane (Parent Context) do clean_text i content.
    """
    chunks = []

    for art in tree:
        act = art.act_code
        art_num = art.article
        act_title = art.act_title
        chpt = art.chapter

        if not art.children:
            full_title = format_chunk_title(act, art_num)
            clean_body = art.text.strip()
            content = (
                f"[AKTYWNE PRAWO]: {act_title}\n"
                f"[ROZDZIAŁ]: {chpt if chpt else 'Główny'}\n"
                f"[JEDNOSTKA]: {full_title}\n\n"
                f"{clean_body}"
            )
            chunks.append({
                "act_code": act,
                "act_title": act_title,
                "chapter": chpt,
                "article_number": art_num,
                "paragraph": None,
                "point": None,
                "letter": None,
                "unit_id": art.unit_id,
                "unit_type": "article",
                "parent_unit_id": None,
                "full_title": full_title,
                "content": content,
                "clean_text": content,
                "year_effective": 2026,
                "status": "OBOWIĄZUJĄCY"
            })
            continue

        for par in art.children:
            par_num = par.paragraph
            par_title = format_chunk_title(act, art_num, par=par_num)

            if not par.children:
                clean_body = par.text.strip()
                intro = f"Wprowadzenie do artykułu: {art.parent_intro}\n\n" if art.parent_intro else ""
                content = (
                    f"[AKTYWNE PRAWO]: {act_title}\n"
                    f"[ROZDZIAŁ]: {chpt if chpt else 'Główny'}\n"
                    f"[JEDNOSTKA]: {par_title}\n\n"
                    f"{intro}{clean_body}"
                )
                chunks.append({
                    "act_code": act,
                    "act_title": act_title,
                    "chapter": chpt,
                    "article_number": art_num,
                    "paragraph": par_num,
                    "point": None,
                    "letter": None,
                    "unit_id": par.unit_id,
                    "unit_type": "paragraph",
                    "parent_unit_id": art.unit_id,
                    "full_title": par_title,
                    "content": content,
                    "clean_text": content,
                    "year_effective": 2026,
                    "status": "OBOWIĄZUJĄCY"
                })
                continue

            # Jeśli ustęp ma punkty:
            # Tworzymy wpis ustępu TYLKO jeśli zawiera istotną treść ogólną/definicyjną (> 35 znaków)
            if par.text and len(par.text.strip()) > 35:
                content = (
                    f"[AKTYWNE PRAWO]: {act_title}\n"
                    f"[ROZDZIAŁ]: {chpt if chpt else 'Główny'}\n"
                    f"[JEDNOSTKA]: {par_title}\n\n"
                    f"Wprowadzenie: {par.text.strip()}"
                )
                chunks.append({
                    "act_code": act,
                    "act_title": act_title,
                    "chapter": chpt,
                    "article_number": art_num,
                    "paragraph": par_num,
                    "point": None,
                    "letter": None,
                    "unit_id": par.unit_id,
                    "unit_type": "paragraph",
                    "parent_unit_id": art.unit_id,
                    "full_title": par_title,
                    "content": content,
                    "clean_text": content,
                    "year_effective": 2026,
                    "status": "OBOWIĄZUJĄCY"
                })

            for pt in par.children:
                pt_num = pt.point
                pt_title = format_chunk_title(act, art_num, par=par_num, pt=pt_num)

                if not pt.children:
                    parent_ctx = f"Kontekst nadrzędny:\n{pt.parent_intro}\n\n" if pt.parent_intro else ""
                    content = (
                        f"[AKTYWNE PRAWO]: {act_title}\n"
                        f"[ROZDZIAŁ]: {chpt if chpt else 'Główny'}\n"
                        f"[JEDNOSTKA]: {pt_title}\n\n"
                        f"{parent_ctx}Treść przepisu:\n{pt.text.strip()}"
                    )
                    chunks.append({
                        "act_code": act,
                        "act_title": act_title,
                        "chapter": chpt,
                        "article_number": art_num,
                        "paragraph": par_num,
                        "point": pt_num,
                        "letter": None,
                        "unit_id": pt.unit_id,
                        "unit_type": "point",
                        "parent_unit_id": par.unit_id,
                        "full_title": pt_title,
                        "content": content,
                        "clean_text": content,
                        "year_effective": 2026,
                        "status": "OBOWIĄZUJĄCY"
                    })
                else:
                    for let in pt.children:
                        let_tok = let.letter
                        let_title = format_chunk_title(act, art_num, par=par_num, pt=pt_num, let=let_tok)
                        parent_ctx = f"Kontekst nadrzędny:\n{let.parent_intro}\n\n" if let.parent_intro else ""
                        content = (
                            f"[AKTYWNE PRAWO]: {act_title}\n"
                            f"[ROZDZIAŁ]: {chpt if chpt else 'Główny'}\n"
                            f"[JEDNOSTKA]: {let_title}\n\n"
                            f"{parent_ctx}Treść przepisu:\n{let.text.strip()}"
                        )
                        chunks.append({
                            "act_code": act,
                            "act_title": act_title,
                            "chapter": chpt,
                            "article_number": art_num,
                            "paragraph": par_num,
                            "point": pt_num,
                            "letter": let_tok,
                            "unit_id": let.unit_id,
                            "unit_type": "letter",
                            "parent_unit_id": pt.unit_id,
                            "full_title": let_title,
                            "content": content,
                            "clean_text": content,
                            "year_effective": 2026,
                            "status": "OBOWIĄZUJĄCY"
                        })

    return chunks


def extract_articles_from_html(file_path: str) -> List[Dict[str, Any]]:
    """Główna funkcja wejściowa parsera: ładuje plik HTML, parsuje DOM i zwraca fragmenty."""
    file_name = os.path.basename(file_path)
    act_code, act_title = ACT_CODE_MAP.get(file_name, (os.path.splitext(file_name)[0].upper(), f"Ustawa ({file_name})"))

    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        html_content = f.read()

    tree = extract_legal_tree(html_content, act_code, act_title)
    chunks = build_retrieval_chunks(tree)

    # Zapewnienie unikalności unit_id w ramach pojedynczego pliku aktu
    unique_chunks = {}
    for c in chunks:
        uid = c["unit_id"]
        if uid not in unique_chunks or len(c["content"]) > len(unique_chunks[uid]["content"]):
            unique_chunks[uid] = c

    return list(unique_chunks.values())
