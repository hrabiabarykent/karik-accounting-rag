import os
import sys
import pytest
from unittest.mock import MagicMock, patch

# Dodanie katalogu głównego do sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from rag.context_cache import get_or_create_context_cache


def test_context_cache_when_client_is_none():
    """Weryfikuje, że podanie client=None zwraca None bez rzucania błędu."""
    result = get_or_create_context_cache(client=None, context_text="Test Ustawy")
    assert result is None


def test_context_cache_reuse_existing():
    """Weryfikuje odzyskiwanie istniejącego aktywnego bufora z chmury."""
    mock_client = MagicMock()
    
    mock_cache = MagicMock()
    mock_cache.display_name = "tax_laws_test_cache"
    mock_cache.name = "caches/existing_cache_12345"
    
    mock_client.caches.list.return_value = [mock_cache]

    result = get_or_create_context_cache(
        client=mock_client,
        context_text="Treść ustaw",
        display_name="tax_laws_test_cache"
    )

    assert result == "caches/existing_cache_12345"
    mock_client.caches.create.assert_not_called()


def test_context_cache_create_new():
    """Weryfikuje tworzenie nowego bufora gdy żaden aktywny nie istnieje."""
    mock_client = MagicMock()
    mock_client.caches.list.return_value = []

    mock_new_cache = MagicMock()
    mock_new_cache.name = "caches/newly_created_cache_67890"
    mock_client.caches.create.return_value = mock_new_cache

    result = get_or_create_context_cache(
        client=mock_client,
        context_text="Treść ustaw z ustawy VAT",
        display_name="tax_laws_test_cache",
        model_name="gemini-2.5-flash",
        ttl_seconds=1800
    )

    assert result == "caches/newly_created_cache_67890"
    mock_client.caches.create.assert_called_once()


def test_context_cache_graceful_fallback_on_exception():
    """Weryfikuje, że awaria API nie niszczy aplikacji i zwraca None (Fallback)."""
    mock_client = MagicMock()
    mock_client.caches.list.side_effect = Exception("API Key quota exceeded or invalid model")

    result = get_or_create_context_cache(
        client=mock_client,
        context_text="Treść",
        display_name="tax_laws_test_cache"
    )

    assert result is None
