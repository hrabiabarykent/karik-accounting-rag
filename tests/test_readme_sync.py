import os
import json
import pytest
from scripts.update_readme_metrics import (
    generate_metrics_markdown,
    update_readme,
    format_pct,
    format_float,
    MARKER_START,
    MARKER_END,
)


@pytest.fixture
def sample_eval_results():
    return {
        "timestamp": "2026-09-10 12:00:00",
        "total_questions": 15,
        "elapsed_seconds": 15.5,
        "metrics": {
            "hit_rate_at_1": 0.7333,
            "hit_rate_at_3": 0.8667,
            "hit_rate_at_5": 0.9333,
            "mrr": 0.8111,
            "hit_rate_at_1_exact": 0.6000,
            "hit_rate_at_3_exact": 0.7333,
            "hit_rate_at_5_exact": 0.8000,
            "mrr_exact": 0.6888,
            "macro_citation_recall": 0.8500,
            "complete_answer_rate": 0.7333,
            "generation": {
                "status": "completed",
                "evaluated_questions": 15,
                "faithfulness": 0.85,
                "heuristic_grounding_score": 0.90
            },
            "faithfulness": 0.85
        },
        "metrics_by_act": {
            "PIT": {
                "questions_count": 7,
                "hit_rate_at_1": 0.7143,
                "hit_rate_at_3": 0.8571,
                "hit_rate_at_5": 1.0,
                "mrr": 0.8095,
                "complete_answer_rate": 0.8571,
                "macro_recall": 0.90
            },
            "VAT": {
                "questions_count": 5,
                "hit_rate_at_1": 0.8000,
                "hit_rate_at_3": 0.8000,
                "hit_rate_at_5": 0.8000,
                "mrr": 0.8000,
                "complete_answer_rate": 0.8000,
                "macro_recall": 0.80
            }
        },
        "detailed_results": []
    }


def test_format_helpers():
    assert format_pct(0.7333) == "73.3%"
    assert format_pct(1.0) == "100.0%"
    assert format_pct(0.0) == "0.0%"
    assert format_pct(None) == "N/A"

    assert format_float(0.8111) == "0.811"
    assert format_float(None) == "N/A"


def test_generate_metrics_markdown(sample_eval_results):
    md = generate_metrics_markdown(sample_eval_results)
    assert MARKER_START in md
    assert MARKER_END in md
    assert "73.3%" in md
    assert "86.7%" in md
    assert "0.811" in md
    assert "PIT" in md
    assert "VAT" in md
    assert "completed" in md


def test_generate_metrics_markdown_offline_not_run(sample_eval_results):
    sample_eval_results["metrics"]["generation"] = {
        "status": "not_run",
        "evaluated_questions": 0,
        "faithfulness": None,
        "heuristic_grounding_score": None
    }
    sample_eval_results["metrics"]["faithfulness"] = None

    md = generate_metrics_markdown(sample_eval_results)
    assert "not_run" in md
    assert "no LLM calls" in md


def test_update_readme_flow(tmp_path, sample_eval_results):
    results_file = tmp_path / "eval_results.json"
    results_file.write_text(json.dumps(sample_eval_results, indent=2), encoding="utf-8")

    initial_readme = (
        "# Title\n\n"
        "## 📊 RAG Benchmark Metrics (Polish Tax Law)\n\n"
        "Old content to be replaced.\n\n"
        "---\n\n"
        "## Other Section\n"
    )
    readme_file = tmp_path / "README.md"
    readme_file.write_text(initial_readme, encoding="utf-8")

    # 1. Weryfikacja: w trybie --check przed aktualizacją powinno zwrócić False
    assert update_readme(str(results_file), str(readme_file), check_only=True) is False

    # 2. Aktualizacja pliku README
    success = update_readme(str(results_file), str(readme_file), check_only=False)
    assert success is True

    # 3. Sprawdzenie, czy markery i treść zostały poprawnie wstrzyknięte
    updated_text = readme_file.read_text(encoding="utf-8")
    assert MARKER_START in updated_text
    assert MARKER_END in updated_text
    assert "## Other Section" in updated_text
    assert "Old content to be replaced" not in updated_text

    # 4. Ponowne sprawdzenie w trybie --check (powinno być True)
    assert update_readme(str(results_file), str(readme_file), check_only=True) is True


def test_update_readme_detects_drift(tmp_path, sample_eval_results, capsys):
    results_file = tmp_path / "eval_results.json"
    results_file.write_text(json.dumps(sample_eval_results, indent=2), encoding="utf-8")

    readme_file = tmp_path / "README.md"
    readme_file.write_text(
        f"# Doc\n\n{MARKER_START}\nOutdated metrics\n{MARKER_END}\n",
        encoding="utf-8"
    )

    # Rozbieżność w trybie check z weryfikacją unified diff
    assert update_readme(str(results_file), str(readme_file), check_only=True) is False
    captured = capsys.readouterr().out
    assert "Unified diff" in captured
    assert "--- README.md" in captured
    assert "+++ eval_results.json" in captured

    # Po uruchomieniu aktualizacji — spójność przywrócona
    assert update_readme(str(results_file), str(readme_file), check_only=False) is True
    assert update_readme(str(results_file), str(readme_file), check_only=True) is True


def test_update_readme_missing_file_raises(tmp_path):
    non_existent = str(tmp_path / "missing.json")
    readme_file = str(tmp_path / "README.md")
    with pytest.raises(FileNotFoundError):
        update_readme(non_existent, readme_file)
