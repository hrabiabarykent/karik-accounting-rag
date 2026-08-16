import os
import sys

# Dodanie katalogu głównego do sys.path dla pytest
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
from rag.parser import extract_articles_from_html
from rag.retriever import sigmoid


def test_html_parsing():
    sample_file = os.path.join(os.path.dirname(__file__), "..", "data", "pobrane_ustawy", "PIT.html")
    assert os.path.exists(sample_file), "Brak pliku PIT.html w data/pobrane_ustawy"

    articles = extract_articles_from_html(sample_file)
    assert len(articles) > 0, "Parser nie wyciągnął żadnych artykułów z PIT.html"
    
    first_art = articles[0]
    assert "act_code" in first_art
    assert first_art["act_code"] == "PIT"
    assert "article_number" in first_art
    assert len(first_art["content"]) > 0

def test_sigmoid_normalization():
    assert sigmoid(0.0) == 0.5
    assert sigmoid(10.0) > 0.99
    assert sigmoid(-10.0) < 0.01
