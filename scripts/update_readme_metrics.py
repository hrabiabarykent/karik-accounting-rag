#!/usr/bin/env python3
"""
scripts/update_readme_metrics.py

Synchronizuje sekcję metryk benchmarku w README.md z wynikami zapisanymi w eval_results.json.
Zapewnia jedno źródło prawdy (Single Source of Truth) dla wyników ewaluacji.

Użycie:
    python scripts/update_readme_metrics.py
    python scripts/update_readme_metrics.py --check
    python scripts/update_readme_metrics.py --results path/to/eval_results.json --readme path/to/README.md
"""

import os
import sys
import json
import argparse
import difflib
from typing import Dict, Any, Optional

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

MARKER_START = "<!-- BENCHMARK_METRICS_START -->"
MARKER_END = "<!-- BENCHMARK_METRICS_END -->"


def format_pct(val: Optional[float], decimals: int = 1) -> str:
    """Formatuje ułamek [0.0, 1.0] na string procentowy."""
    if val is None:
        return "N/A"
    return f"{val * 100:.{decimals}f}%"


def format_float(val: Optional[float], decimals: int = 3) -> str:
    """Formatuje liczbę zmiennoprzecinkową."""
    if val is None:
        return "N/A"
    return f"{val:.{decimals}f}"


def generate_metrics_markdown(results_data: Dict[str, Any]) -> str:
    """Generuje blok Markdown na podstawie danych z eval_results.json."""
    metrics = results_data.get("metrics", {})
    metrics_by_act = results_data.get("metrics_by_act", {})
    timestamp = results_data.get("timestamp", "nieznany")
    total_q = results_data.get("total_questions", len(results_data.get("detailed_results", [])))

    h1 = metrics.get("hit_rate_at_1")
    h3 = metrics.get("hit_rate_at_3")
    h5 = metrics.get("hit_rate_at_5")
    mrr = metrics.get("mrr")

    h1_ex = metrics.get("hit_rate_1_exact", metrics.get("hit_rate_at_1_exact"))
    h3_ex = metrics.get("hit_rate_3_exact", metrics.get("hit_rate_at_3_exact"))
    h5_ex = metrics.get("hit_rate_5_exact", metrics.get("hit_rate_at_5_exact"))
    mrr_ex = metrics.get("reciprocal_rank_exact", metrics.get("mrr_exact"))

    recall = metrics.get("macro_citation_recall")
    compl = metrics.get("complete_answer_rate")

    gen_info = metrics.get("generation", {})
    gen_status = gen_info.get("status", "not_run" if metrics.get("faithfulness") == 0.0 else "completed")
    faith = gen_info.get("faithfulness", metrics.get("faithfulness"))
    grounding = gen_info.get("heuristic_grounding_score")

    if gen_status == "not_run" or faith is None:
        faith_str = f"`not_run` (brak wywołania LLM w trybie offline/CI)"
    else:
        faith_str = f"**{format_pct(faith)}** (status: `{gen_status}`)"

    if gen_status == "not_run" or grounding is None:
        grounding_str = f"`not_run` (brak wywołania LLM w trybie offline/CI)"
    else:
        grounding_str = f"**{format_pct(grounding)}**"

    lines = [
        MARKER_START,
        f"*Ostatnia ewaluacja benchmarku: `{timestamp}`* | *Liczba scenariuszy testowych: `{total_q}`*",
        "",
        "### 🎯 Skuteczność Retrievalu i Pokrycia Cytowań",
        "",
        "| Metryka Wyszukiwania | Poziom Artykułu | Poziom Ścisły (Exact) | Znaczenie Biznesowe |",
        "| :--- | :---: | :---: | :--- |",
        f"| **Hit Rate @ 1** | **{format_pct(h1)}** | **{format_pct(h1_ex)}** | Odnalezienie właściwego przepisu na 1. pozycji |",
        f"| **Hit Rate @ 3** | **{format_pct(h3)}** | **{format_pct(h3_ex)}** | Obecność właściwego przepisu w Top 3 |",
        f"| **Hit Rate @ 5** | **{format_pct(h5)}** | **{format_pct(h5_ex)}** | Obecność właściwego przepisu w Top 5 |",
        f"| **MRR (Mean Reciprocal Rank)** | **{format_float(mrr)}** | **{format_float(mrr_ex)}** | Średnia odwrotność rangi pierwszego trafienia |",
        f"| **Macro Citation Recall** | **{format_pct(recall)}** | — | Średnie pokrycie wymaganych jednostek redakcyjnych |",
        f"| **Complete-Answer Rate** | **{format_pct(compl)}** | — | Odsetek pytań z kompletnym zestawem przepisów |",
        "",
        "### 🧠 Jakość Generacji i Weryfikacja Ugruntowania",
        f"- **Status ewaluacji generacji**: `{gen_status}`",
        f"- **Faithfulness (Wierność Semantyczna)**: {faith_str}",
        f"- **Heuristic Grounding Score**: {grounding_str}",
    ]

    if metrics_by_act:
        lines.extend([
            "",
            "### 🏛️ Rozbicie Wyników per Akt Prawny",
            "",
            "| Akt Prawny | Liczba Pytań | Hit Rate @ 3 | Complete-Answer | MRR |",
            "| :--- | :---: | :---: | :---: | :---: |",
        ])
        for act, act_m in sorted(metrics_by_act.items()):
            lines.append(
                f"| **{act}** | {act_m.get('questions_count', 0)} | "
                f"{format_pct(act_m.get('hit_rate_at_3'))} | "
                f"{format_pct(act_m.get('complete_answer_rate'))} | "
                f"{format_float(act_m.get('mrr'))} |"
            )

    lines.append(MARKER_END)
    return "\n".join(lines)


def update_readme(
    results_path: str,
    readme_path: str,
    check_only: bool = False
) -> bool:
    """
    Aktualizuje lub weryfikuje zawartość README.md pod kątem spójności z eval_results.json.
    Zwraca True, jeśli plik jest aktualny / został zaktualizowany, lub False jeśli w trybie check wykryto rozbieżność.
    """
    if not os.path.exists(results_path):
        raise FileNotFoundError(f"Nie odnaleziono pliku z wynikami benchmarku: {results_path}")

    if not os.path.exists(readme_path):
        raise FileNotFoundError(f"Nie odnaleziono pliku README: {readme_path}")

    with open(results_path, "r", encoding="utf-8") as f:
        results_data = json.load(f)

    with open(readme_path, "r", encoding="utf-8") as f:
        readme_content = f.read()

    new_block = generate_metrics_markdown(results_data)

    if MARKER_START in readme_content and MARKER_END in readme_content:
        start_idx = readme_content.find(MARKER_START)
        end_idx = readme_content.find(MARKER_END) + len(MARKER_END)
        current_block = readme_content[start_idx:end_idx]

        if current_block == new_block:
            print("✅ README.md jest w 100% zsynchronizowany z eval_results.json.")
            return True

        if check_only:
            print("❌ BŁĄD: README.md nie jest zsynchronizowany z eval_results.json!\n")
            print("--- Unified diff (README.md vs eval_results.json) ---")
            diff = difflib.unified_diff(
                current_block.splitlines(keepends=True),
                new_block.splitlines(keepends=True),
                fromfile="README.md (aktualny)",
                tofile="eval_results.json (oczekiwany)",
                n=3,
            )
            diff_text = "".join(diff)
            print(diff_text if diff_text else "(Brak bezpośrednich różnic znakowych)")
            return False

        updated_content = readme_content[:start_idx] + new_block + readme_content[end_idx:]
    else:
        # Fallback: zastąpienie sekcji nagłówka RAG Benchmark Metrics
        header_target = "## 📊 RAG Benchmark Metrics (Polish Tax Law)"
        if header_target in readme_content:
            parts = readme_content.split(header_target, 1)
            # Szukamy następnego nagłówka '---' lub '##'
            sub = parts[1]
            next_section = sub.find("\n---")
            if next_section == -1:
                next_section = sub.find("\n## ")

            if next_section != -1:
                remainder = sub[next_section:]
            else:
                remainder = ""

            updated_content = parts[0] + header_target + "\n\n" + new_block + remainder
        else:
            raise ValueError(f"Nie znaleziono znaczników {MARKER_START} ani nagłówka '{header_target}' w {readme_path}")

        if check_only:
            print(f"❌ BŁĄD: README.md nie posiada znaczników synchronizacji {MARKER_START}!")
            return False

    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(updated_content)

    print(f"✅ Pomyślnie zaktualizowano metryki benchmarku w {os.path.abspath(readme_path)}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Synchronizacja metryk benchmarku RAG z README.md")
    parser.add_argument("--results", default="eval_results.json", help="Ścieżka do pliku eval_results.json")
    parser.add_argument("--readme", default="README.md", help="Ścieżka do pliku README.md")
    parser.add_argument("--check", action="store_true", help="Tryb sprawdzania spójności (zwraca kod 1 przy braku zgodności)")

    args = parser.parse_args()

    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    results_path = os.path.join(base_dir, args.results) if not os.path.isabs(args.results) else args.results
    readme_path = os.path.join(base_dir, args.readme) if not os.path.isabs(args.readme) else args.readme

    try:
        success = update_readme(results_path=results_path, readme_path=readme_path, check_only=args.check)
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"KRYTYCZNY BŁĄD synchronizacji README: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
