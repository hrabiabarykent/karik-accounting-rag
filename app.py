import os
import sys
import re
import json
import logging
import streamlit as st

# Dodanie katalogu głównego do sys.path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

def _load_env():
    env_file = os.path.join(os.path.abspath(os.path.dirname(__file__)), ".env")
    if os.path.exists(env_file):
        try:
            from dotenv import load_dotenv
            load_dotenv(env_file, override=True)
        except ImportError:
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ[k.strip()] = v.strip().strip("'\"")

_load_env()

from rag.pipeline import run_rag_pipeline, build_prompt_and_context
from pii_sanitizer import PresidioInvoiceSanitizer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# =====================================================================
# 1. KONFIGURACJA STRONY I AUTORSKI MOTYW (NORDIC LEGAL EMERALD)
# =====================================================================

st.set_page_config(
    page_title="KARIK RAG - Asystent Prawno-Podatkowy",

    page_icon="⚖️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Unikalna estetyka: Nordic Legal Emerald Dark Mode (głęboki szmaragd, mint #2dd4bf, złoto #f59e0b)
NORDIC_CSS = """
<style>
    /* Ogólne tło aplikacji */
    .stApp {
        background-color: #0f1715;
        color: #e2e8f0;
        font-family: 'Inter', system-ui, -apple-system, sans-serif;
    }

    /* Pasek boczny Sidebar */
    [data-testid="stSidebar"] {
        background-color: #0d1514;
        border-right: 1px solid rgba(45, 212, 191, 0.15);
    }
    
    [data-testid="stSidebar"] > div:first-child {
        padding-top: 1.5rem;
    }

    /* Brand Header w Sidebarze */
    .sidebar-brand {
        display: flex;
        align-items: center;
        gap: 14px;
        padding: 12px 14px;
        background: linear-gradient(135deg, rgba(20, 32, 29, 0.95) 0%, rgba(13, 148, 136, 0.15) 100%);
        border: 1px solid rgba(45, 212, 191, 0.25);
        border-radius: 14px;
        margin-bottom: 18px;
        box-shadow: 0 4px 20px rgba(0, 0, 0, 0.25);
    }

    .brand-icon {
        width: 38px;
        height: 38px;
        background: linear-gradient(135deg, #0d9488 0%, #2dd4bf 100%);
        border-radius: 10px;
        display: flex;
        align-items: center;
        justify-content: center;
        box-shadow: 0 0 15px rgba(45, 212, 191, 0.4);
        flex-shrink: 0;
    }

    .brand-title {
        font-size: 1.25rem;
        font-weight: 800;
        letter-spacing: -0.5px;
        color: #ffffff;
        line-height: 1.1;
    }

    .brand-subtitle {
        font-size: 0.72rem;
        color: #2dd4bf;
        font-weight: 600;
        letter-spacing: 0.5px;
        text-transform: uppercase;
        margin-top: 2px;
    }

    .brand-badge {
        font-size: 0.65rem;
        background: rgba(45, 212, 191, 0.15);
        color: #5eead4;
        border: 1px solid rgba(45, 212, 191, 0.3);
        padding: 1px 6px;
        border-radius: 6px;
        margin-left: 6px;
    }

    /* Karta statusowa w pasku bocznym */
    .sidebar-card {
        background: rgba(18, 28, 26, 0.7);
        border: 1px solid rgba(45, 212, 191, 0.15);
        border-radius: 12px;
        padding: 12px 14px;
        margin-top: 10px;
        margin-bottom: 12px;
    }

    .sidebar-card-title {
        font-size: 0.75rem;
        font-weight: 700;
        color: #94a3b8;
        text-transform: uppercase;
        letter-spacing: 0.6px;
        margin-bottom: 10px;
        display: flex;
        align-items: center;
        gap: 6px;
    }

    .status-row {
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: 6px 0;
        border-bottom: 1px solid rgba(255, 255, 255, 0.04);
        font-size: 0.8rem;
    }

    .status-row:last-child {
        border-bottom: none;
    }

    .status-label {
        color: #cbd5e1;
        font-weight: 500;
    }

    .status-badge {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        font-size: 0.75rem;
        font-weight: 600;
        color: #34d399;
        background: rgba(16, 185, 129, 0.1);
        padding: 2px 8px;
        border-radius: 10px;
        border: 1px solid rgba(52, 211, 153, 0.2);
    }

    /* Pulsujący wskaźnik statusu */
    .pulse-dot {
        width: 7px;
        height: 7px;
        border-radius: 50%;
        background-color: #10b981;
        box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7);
        animation: pulse-green 2s infinite;
        display: inline-block;
    }

    @keyframes pulse-green {
        0% {
            transform: scale(0.95);
            box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7);
        }
        70% {
            transform: scale(1);
            box-shadow: 0 0 0 6px rgba(16, 185, 129, 0);
        }
        100% {
            transform: scale(0.95);
            box-shadow: 0 0 0 0 rgba(16, 185, 129, 0);
        }
    }

    /* Sekcja wątków czatu */
    .sidebar-section-title {
        font-size: 0.78rem;
        font-weight: 700;
        color: #2dd4bf;
        text-transform: uppercase;
        letter-spacing: 0.8px;
        margin-top: 14px;
        margin-bottom: 8px;
    }

    /* Nagłówek aplikacji */
    .main-header {
        background: linear-gradient(135deg, rgba(20, 32, 29, 0.9) 0%, rgba(13, 148, 136, 0.2) 100%);
        border: 1px solid rgba(45, 212, 191, 0.25);
        border-radius: 16px;
        padding: 24px;
        margin-bottom: 24px;
        backdrop-filter: blur(12px);
    }

    .main-title {
        color: #2dd4bf;
        font-size: 2.2rem;
        font-weight: 800;
        margin: 0;
        letter-spacing: -0.5px;
    }

    .main-subtitle {
        color: #94a3b8;
        font-size: 1.0rem;
        margin-top: 6px;
    }

    /* Badge statusowe w podtytule */
    .badge-gpu {
        background-color: rgba(16, 185, 129, 0.15);
        color: #34d399;
        border: 1px solid rgba(52, 211, 153, 0.3);
        padding: 4px 10px;
        border-radius: 20px;
        font-size: 0.82rem;
        font-weight: 600;
        display: inline-block;
        margin-right: 8px;
    }

    .badge-db {
        background-color: rgba(245, 158, 11, 0.15);
        color: #fbbf24;
        border: 1px solid rgba(251, 191, 36, 0.3);
        padding: 4px 10px;
        border-radius: 20px;
        font-size: 0.82rem;
        font-weight: 600;
        display: inline-block;
    }

    /* Karty wiadomości w czacie */
    .stChatMessage {
        background-color: rgba(20, 32, 29, 0.6) !important;
        border: 1px solid rgba(45, 212, 191, 0.15) !important;
        border-radius: 12px !important;
        padding: 16px !important;
        margin-bottom: 12px !important;
    }

    /* Kolory Risk Level Badges */
    .risk-badge-low {
        background-color: rgba(16, 185, 129, 0.2);
        color: #34d399;
        border: 1px solid #10b981;
        padding: 6px 14px;
        border-radius: 8px;
        font-weight: 700;
        display: inline-block;
        margin-bottom: 12px;
    }

    .risk-badge-medium {
        background-color: rgba(245, 158, 11, 0.2);
        color: #fbbf24;
        border: 1px solid #f59e0b;
        padding: 6px 14px;
        border-radius: 8px;
        font-weight: 700;
        display: inline-block;
        margin-bottom: 12px;
    }

    .risk-badge-high {
        background-color: rgba(239, 68, 68, 0.2);
        color: #f87171;
        border: 1px solid #ef4444;
        padding: 6px 14px;
        border-radius: 8px;
        font-weight: 700;
        display: inline-block;
        margin-bottom: 12px;
    }

    /* Pola rozwijane Expander */
    .stExpander {
        background-color: rgba(15, 23, 21, 0.8) !important;
        border: 1px solid rgba(45, 212, 191, 0.2) !important;
        border-radius: 10px !important;
        margin-top: 10px !important;
    }

    /* Przycisk wysyłania chat input */
    .stChatInputContainer input {
        background-color: #14201d !important;
        color: #f8fafc !important;
        border: 1px solid rgba(45, 212, 191, 0.3) !important;
        border-radius: 10px !important;
    }

    /* Stylizacja przycisków */
    div.stButton > button {
        background: linear-gradient(135deg, #0d9488 0%, #14b8a6 100%);
        color: #ffffff;
        font-weight: 600;
        border-radius: 8px;
        border: none;
        padding: 8px 16px;
        transition: all 0.2s ease;
    }
    div.stButton > button:hover {
        background: linear-gradient(135deg, #14b8a6 0%, #2dd4bf 100%);
        box-shadow: 0 4px 12px rgba(45, 212, 191, 0.3);
    }
</style>
"""

st.markdown(NORDIC_CSS, unsafe_allow_html=True)

# =====================================================================
import time

# =====================================================================
# 2. INICJALIZACJA STANU WIELE SESJI CZATÓW (MULTI-SESSION CHAT MANAGEMENT)
# =====================================================================

if "chats" not in st.session_state or not st.session_state.chats:
    st.session_state.chats = {
        "chat_1": {
            "title": "Główna Analiza Podatkowa",
            "messages": [{
                "role": "assistant",
                "content": "Dzień dobry! Jesteś w systemie **KARIK RAG** – eksperckim asystencie prawa podatkowo-księgowego.\n\nWszystkie Twoje pytania są automatycznie anonimizowane zgodnie z **RODO** (Presidio + SpaCy `pl_core_news_lg`), a odpowiedzi opierają się na **3 724 wyekstrahowanych artykułach prawnych** z baz Sejmu ELI i ISAP.\n\nW czym mogę dzisiaj pomóc?",
                "cited_articles": [],
                "risk_level": "NISKIE",
                "risk_justification": "Gotowość operacyjna systemu.",
                "prompt_to_copy": ""
            }]
        }
    }

if "active_chat_id" not in st.session_state or st.session_state.active_chat_id not in st.session_state.chats:
    st.session_state.active_chat_id = list(st.session_state.chats.keys())[0]

# Podpięcie obecnego wątku
current_chat = st.session_state.chats[st.session_state.active_chat_id]
active_messages = current_chat["messages"]

# =====================================================================
# 3. PASEK BOCZNY (SIDEBAR) - STATUS SYSTEMU & LISTA SPRAW (CZATÓW)
# =====================================================================

with st.sidebar:
    # Nowoczesny Brand Header z wektorowym logotypem
    st.markdown("""
    <div class="sidebar-brand">
        <div class="brand-icon">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#ffffff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/>
            </svg>
        </div>
        <div>
            <div class="brand-title">KARIK<span class="brand-badge">v3.0</span></div>
            <div class="brand-subtitle">Legal & Tax RAG Engine</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # Przycisk tworzenia nowej sprawy
    if st.button("＋ Nowa Analiza Podatkowa", use_container_width=True, type="primary"):
        new_id = f"chat_{len(st.session_state.chats) + 1}_{int(time.time())}"
        st.session_state.chats[new_id] = {
            "title": f"Sprawa #{len(st.session_state.chats) + 1}",
            "messages": [{
                "role": "assistant",
                "content": "Jesteś w nowym wątku analizy podatkowej. Zadaj pytanie klienta, a system przygotuje zdetokenizowaną odpowiedź z powiązanymi artykułami prawnymi.",
                "cited_articles": [],
                "risk_level": "NISKIE",
                "risk_justification": "Nowa sesja analizy.",
                "prompt_to_copy": ""
            }]
        }
        st.session_state.active_chat_id = new_id
        st.rerun()

    st.markdown('<div class="sidebar-section-title">Wątki Analiz</div>', unsafe_allow_html=True)

    # Wyświetlanie listy aktywnych spraw z poprawioną logiką usuwania
    for cid, cdata in list(st.session_state.chats.items()):
        is_active = (cid == st.session_state.active_chat_id)
        col1, col2 = st.columns([0.82, 0.18])
        with col1:
            prefix = "● " if is_active else "○ "
            if st.button(f"{prefix}{cdata['title']}", key=f"btn_{cid}", use_container_width=True):
                st.session_state.active_chat_id = cid
                st.rerun()
        with col2:
            if len(st.session_state.chats) > 1:
                if st.button("✕", key=f"del_{cid}", help="Usuń ten wątek"):
                    was_active = (cid == st.session_state.active_chat_id)
                    del st.session_state.chats[cid]
                    if was_active:
                        st.session_state.active_chat_id = list(st.session_state.chats.keys())[0]
                    st.rerun()

    # Karta statusu architektury z pulsującymi wskaźnikami
    st.markdown("""
    <div class="sidebar-card">
        <div class="sidebar-card-title">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#2dd4bf" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>
            Status Architektury
        </div>
        <div class="status-row">
            <span class="status-label">RODO Zero-Trust</span>
            <span class="status-badge"><span class="pulse-dot"></span>Presidio + SpaCy</span>
        </div>
        <div class="status-row">
            <span class="status-label">Lokalny SLM</span>
            <span class="status-badge"><span class="pulse-dot"></span>Gemma 4 CUDA</span>
        </div>
        <div class="status-row">
            <span class="status-label">Reranker NLP</span>
            <span class="status-badge"><span class="pulse-dot"></span>RoBERTa-v3</span>
        </div>
        <div class="status-row">
            <span class="status-label">Baza Wektorowa</span>
            <span class="status-badge"><span class="pulse-dot"></span>pgvector (3 724 Art.)</span>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # Ustawienia widoku
    st.markdown('<div class="sidebar-section-title">Opcje Widoku</div>', unsafe_allow_html=True)
    show_rodo_details = st.checkbox("Podgląd anonimizacji RODO", value=True)
    show_prompt_debug = st.checkbox("Prompter RAG (Debug)", value=False)

    st.markdown("<div style='margin-top: 12px;'></div>", unsafe_allow_html=True)
    if st.button("Wyczyść historię wątku", use_container_width=True):
        st.session_state.chats[st.session_state.active_chat_id]["messages"] = []
        st.rerun()


# =====================================================================
# 4. NAGŁÓWEK GŁÓWNY (MAIN HEADER)
# =====================================================================

st.markdown("""
<div class="main-header">
    <div class="main-title">KARIK RAG</div>
    <div class="main-subtitle">Inteligentny Asystent Prawa Podatkowego & Księgowości z Warstwą Izolacji RODO</div>
    <div style="margin-top: 14px; display: flex; gap: 8px; flex-wrap: wrap;">
        <span class="badge-gpu">NVIDIA CUDA ACCELERATION</span>
        <span class="badge-db">BAZA AKTÓW • 3 724 ARTYKUŁY</span>
    </div>
</div>
""", unsafe_allow_html=True)

# =====================================================================
# 5. WYŚWIETLANIE HISTORII CZATU
# =====================================================================

for msg in active_messages:
    with st.chat_message(msg["role"]):
        # Badge ryzyka dla odpowiedzi asystenta
        if msg["role"] == "assistant" and msg.get("risk_level"):
            risk = msg.get("risk_level", "ŚREDNIE")
            badge_class = "risk-badge-low" if risk == "NISKIE" else ("risk-badge-medium" if risk == "ŚREDNIE" else "risk-badge-high")
            st.markdown(f'<div class="{badge_class}">Ryzyko Podatkowe: {risk}</div>', unsafe_allow_html=True)
            if msg.get("risk_justification"):
                st.caption(f"**Uzasadnienie ryzyka**: {msg['risk_justification']}")

        st.markdown(msg["content"])

        # Cytowane odnośniki do ustaw z bezpośrednim podglądem w czacie
        if msg.get("cited_articles"):
            with st.expander("Cytowane Przepisy Prawne (Podgląd w Aplikacji & Odnośniki)"):
                for art in msg["cited_articles"]:
                    st.markdown(f"• **{art['full_title']}** (Trafność Rerankera: `{art['rerank_score']:.4f}`)")
                    article_text = art.get("text") or f"[Przepis {art.get('full_title')}]\n\nTreść artykułu dostępna w pełnym pliku ustawy poniżej."
                    with st.expander(f"Treść artykułu ({art['act_code']} Art. {art['article_number']})"):
                        st.markdown(f"```text\n{article_text}\n```")
                    st.markdown(f'<div style="margin-left: 20px; margin-bottom: 8px;"><a href="{art["file_link"]}" target="_blank" style="color: #2dd4bf; text-decoration: underline; font-weight: 500;">🔗 Otwórz w osobnym oknie HTML ({art["act_code"]} Art. {art["article_number"]})</a></div>', unsafe_allow_html=True)

        # Podgląd promptera dla celów debugowania/skopiowania do Chatu Gemini Online
        if show_prompt_debug and msg.get("prompt_to_copy"):
            with st.expander("Wygenerowany Prompt dla Chatu Gemini Online (Copy/Paste)"):
                st.code(msg["prompt_to_copy"], language="text")

# =====================================================================
# 6. POLE INPUTU UŻYTKOWNIKA I WYKONANIE POTOKU RAG
# =====================================================================

user_input = st.chat_input("Wpisz pytanie podatkowe (np. Koszty amortyzacji auta 220k PLN i 50% VAT od paliwa)...")

if user_input:
    # Automatyczna zmiana nazwy sprawy po pierwszym pytaniu
    if len([m for m in active_messages if m["role"] == "user"]) == 0:
        short_title = user_input[:28] + ("..." if len(user_input) > 28 else "")
        current_chat["title"] = short_title

    # Zapisz wiadomość użytkownika
    active_messages.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    # Wykonanie potoku z animowanym stadem statusowym
    with st.chat_message("assistant"):
        with st.status("Przetwarzanie potoku KARIK RAG...", expanded=True) as status:

            st.write("1. **Anonimizacja RODO**: Presidio + SpaCy `pl_core_news_lg`...")
            st.write("2. **Gemma SLM Query Rewriting**: Transformacja zapytania na frazy ustawowe...")
            st.write("3. **pgvector + CUDA Reranker**: Przeszukiwanie 3 724 ustępów prawnych...")
            
            rag_result = run_rag_pipeline(user_query=user_input)
            
            st.write("4. **Synteza LLM & Detokenizacja**: Rekompilacja danych klienta...")
            status.update(label="✓ Potok RAG wykonany pomyślnie!", state="complete", expanded=False)

        # Wyświetlenie odkodowanej odpowiedzi dla księgowego
        risk = rag_result.get("risk_level", "ŚREDNIE")
        badge_class = "risk-badge-low" if risk == "NISKIE" else ("risk-badge-medium" if risk == "ŚREDNIE" else "risk-badge-high")
        
        st.markdown(f'<div class="{badge_class}">Ryzyko Podatkowe: {risk}</div>', unsafe_allow_html=True)
        if rag_result.get("risk_justification"):
            st.caption(f"**Uzasadnienie ryzyka**: {rag_result['risk_justification']}")

        answer_text = rag_result.get("answer_text", "")
        st.markdown(answer_text)

        # Cytowane odnośniki do ustaw z bezpośrednim podglądem w czacie
        cited_articles = rag_result.get("cited_articles", [])
        if cited_articles:
            with st.expander("Cytowane Przepisy Prawne (Podgląd w Aplikacji & Odnośniki)", expanded=True):
                for art in cited_articles:
                    st.markdown(f"• **{art['full_title']}** (Trafność Rerankera: `{art['rerank_score']:.4f}`)")
                    article_text = art.get("text") or f"[Przepis {art.get('full_title')}]\n\nTreść artykułu dostępna w pełnym pliku ustawy poniżej."
                    with st.expander(f"Treść artykułu ({art['act_code']} Art. {art['article_number']})"):
                        st.markdown(f"```text\n{article_text}\n```")
                    st.markdown(f'<div style="margin-left: 20px; margin-bottom: 8px;"><a href="{art["file_link"]}" target="_blank" style="color: #2dd4bf; text-decoration: underline; font-weight: 500;">🔗 Otwórz w osobnym oknie HTML ({art["act_code"]} Art. {art["article_number"]})</a></div>', unsafe_allow_html=True)

        # Podgląd RODO jeśli włączony
        if show_rodo_details:
            with st.expander("Podgląd Anonimizacji RODO (Zapytanie zanonimizowane)"):
                st.markdown(f"**Zapytanie zanonimizowane wysłane poza firmę**:")
                st.code(rag_result.get("anonymized_query", ""), language="text")
                st.caption(f"Liczba zanonimizowanych tokenów RODO: {rag_result.get('pii_mapping_count', 0)}")

        # Podgląd promptera dla kopiowania
        if show_prompt_debug:
            with st.expander("Wygenerowany Prompt RAG (Copy/Paste do Gemini Chat Online)"):
                st.code(rag_result.get("prompt_to_copy", ""), language="text")

        # Zapisz odpowiedź w stanie sesji
        active_messages.append({
            "role": "assistant",
            "content": answer_text,
            "cited_articles": cited_articles,
            "risk_level": risk,
            "risk_justification": rag_result.get("risk_justification", ""),
            "prompt_to_copy": rag_result.get("prompt_to_copy", ""),
            "user_query": user_input
        })
        st.rerun()

# Dedykowana sekcja wklejania odpowiedzi z Gemini Online (pojawia się tylko po zadaniu pytania w trybie bez API Key)
has_user_message = any(m["role"] == "user" for m in active_messages)
last_msg = active_messages[-1] if active_messages else None
show_paste_box = has_user_message and last_msg and last_msg["role"] == "assistant" and ("Tryb testowy" in last_msg.get("content", "") or not os.getenv("GEMINI_API_KEY"))

if show_paste_box:
    st.markdown("---")
    with st.expander("📥 **WKLEJ ODPOWIEDŹ Z GEMINI CHAT ONLINE (TRYB BEZ BEZPOŚREDNIEGO API)**", expanded=True):
        st.markdown("1. Skopiuj prompt z pola **`📋 Wygenerowany Prompt RAG`** powyżej.")
        st.markdown("2. Wklej go w bezpłatnym chacie **Gemini Online / ChatGPT**.")
        st.markdown("3. Odpowiedź z Chatu Gemini wklej w poniższe pole i kliknij **Przetwórz Odpowiedź**:")

        pasted_llm_response = st.text_area(
            "Treść odpowiedzi z Chatu Gemini Online:",
            value="",
            height=150,
            placeholder="Wklej tutaj odpowiedź skopiowaną z Chatu Gemini...",
            key="manual_pasted_response_input"
        )

        if st.button("🚀 Przetwórz i Detokenizuj Odpowiedź dla Księgowego", use_container_width=True):
            if not pasted_llm_response.strip():
                st.warning("Najpierw wklej treść odpowiedzi z Chatu Gemini Online w powyższe pole!")
            else:
                last_query = "Jak zaksięgować amortyzację auta 220k PLN i odliczyć 50% VAT od paliwa?"
                for m in reversed(active_messages):
                    if m["role"] == "user":
                        last_query = m["content"]
                        break

                with st.spinner("Detokenizacja RODO i wyliczanie ryzyka..."):
                    final_result = run_rag_pipeline(user_query=last_query, manual_llm_response=pasted_llm_response)

                # Podmieniamy ostatnią wiadomość testową na pełną zdetokenizowaną odpowiedź
                risk = final_result.get("risk_level", "ŚREDNIE")
                active_messages[-1] = {
                    "role": "assistant",
                    "content": final_result.get("answer_text", ""),
                    "cited_articles": final_result.get("cited_articles", []),
                    "risk_level": risk,
                    "risk_justification": final_result.get("risk_justification", ""),
                    "prompt_to_copy": final_result.get("prompt_to_copy", "")
                }
                st.success("✓ Odpowiedź została zdetokenizowana i przetworzona!")
                st.rerun()



