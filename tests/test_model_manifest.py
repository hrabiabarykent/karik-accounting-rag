import hashlib
import sys
from pathlib import Path
import pytest

from scripts.download_models import (
    MODEL_MANIFEST,
    compute_sha256,
    verify_model_files,
    find_snapshot_dir,
    download_or_verify_model,
)


def test_manifest_structure():
    """Weryfikuje strukturę manifestu oraz format sum kontrolnych SHA-256."""
    assert "sdadas/mmlw-e5-base" in MODEL_MANIFEST
    assert "sdadas/polish-reranker-roberta-v3" in MODEL_MANIFEST

    for model_key, info in MODEL_MANIFEST.items():
        assert "repo_id" in info
        assert "revision" in info
        assert "files" in info
        assert len(info["revision"]) == 40  # Pełny Git commit hash
        assert "model.safetensors" in info["files"]
        assert "config.json" in info["files"]

        for filename, sha256_hash in info["files"].items():
            assert len(sha256_hash) == 64, f"Niepoprawna długość SHA-256 dla {model_key}::{filename}"
            # Sprawdzenie czy to poprawny hex
            int(sha256_hash, 16)


def test_compute_sha256(tmp_path):
    """Testuje poprawność funkcji compute_sha256 względem wzorca hashlib."""
    test_file = tmp_path / "sample.bin"
    payload = b"KARIK deterministic offline model verification payload 12345"
    test_file.write_bytes(payload)

    expected_hash = hashlib.sha256(payload).hexdigest()
    assert compute_sha256(test_file) == expected_hash


def test_verify_model_files_detection(tmp_path):
    """Testuje wykrywanie poprawnych plików, brakujących plików oraz zmodyfikowanych wag."""
    f1 = tmp_path / "model.safetensors"
    f2 = tmp_path / "config.json"

    f1.write_bytes(b"valid_weights")
    f2.write_bytes(b"valid_config")

    hash1 = hashlib.sha256(b"valid_weights").hexdigest()
    hash2 = hashlib.sha256(b"valid_config").hexdigest()

    expected = {
        "model.safetensors": hash1,
        "config.json": hash2,
    }

    # 1. Poprawny stan
    ok, errors = verify_model_files(tmp_path, expected)
    assert ok is True
    assert len(errors) == 0

    # 2. Zmodyfikowana zawartość (tampering / corruption)
    f1.write_bytes(b"corrupted_weights")
    ok, errors = verify_model_files(tmp_path, expected)
    assert ok is False
    assert any("Niezgodność SHA-256 dla model.safetensors" in e for e in errors)

    # 3. Brakujący plik
    f2.unlink()
    ok, errors = verify_model_files(tmp_path, expected)
    assert ok is False
    assert any("Brak pliku: config.json" in e for e in errors)


def test_find_snapshot_dir(tmp_path):
    """Weryfikuje odnajdywanie katalogu snapshotu w strukturze cache."""
    cache_dir = tmp_path / "hf_cache"
    repo_id = "test-org/test-model"
    revision = "abcdef1234567890abcdef1234567890abcdef12"

    snapshot_path = cache_dir / "models--test-org--test-model" / "snapshots" / revision
    snapshot_path.mkdir(parents=True)

    found = find_snapshot_dir(repo_id, revision, cache_dir)
    assert found == snapshot_path

    # Przypadek braku katalogu
    not_found = find_snapshot_dir("other-org/other-model", revision, cache_dir)
    assert not_found is None


def test_download_or_verify_model_verify_only_missing(tmp_path):
    """Weryfikuje zachowanie w trybie verify_only przy braku modelu w podanym cache."""
    empty_cache = tmp_path / "empty_cache"
    empty_cache.mkdir()

    result = download_or_verify_model(
        model_key="sdadas/mmlw-e5-base",
        cache_dir=empty_cache,
        verify_only=True,
    )
    assert result is False


def test_manifest_completeness():
    """Weryfikuje kompletność zestawu plików dla obu modeli w manifeście."""
    mmlw_files = MODEL_MANIFEST["sdadas/mmlw-e5-base"]["files"]
    expected_mmlw_keys = {
        "model.safetensors",
        "config.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
        "sentencepiece.bpe.model",
        "modules.json",
        "config_sentence_transformers.json",
        "sentence_bert_config.json",
        "1_Pooling/config.json",
    }
    assert expected_mmlw_keys.issubset(set(mmlw_files.keys()))

    reranker_files = MODEL_MANIFEST["sdadas/polish-reranker-roberta-v3"]["files"]
    expected_reranker_keys = {
        "model.safetensors",
        "config.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
        "added_tokens.json",
    }
    assert expected_reranker_keys.issubset(set(reranker_files.keys()))


def test_offline_model_loading():
    """Weryfikuje, że modele ładują się i inferują w trybie 100% offline (local_files_only=True)."""
    mmlw_info = MODEL_MANIFEST["sdadas/mmlw-e5-base"]
    reranker_info = MODEL_MANIFEST["sdadas/polish-reranker-roberta-v3"]

    mmlw_dir = find_snapshot_dir(mmlw_info["repo_id"], mmlw_info["revision"])
    reranker_dir = find_snapshot_dir(reranker_info["repo_id"], reranker_info["revision"])

    if not mmlw_dir or not reranker_dir:
        pytest.skip("Modele NLP nie są pobrane do lokalnego cache - pomijanie testu offline inferencji.")

    try:
        from sentence_transformers import SentenceTransformer, CrossEncoder
    except ImportError:
        pytest.skip("Brak pakietu sentence_transformers w środowisku testowym.")

    # Test ładowania offline SentenceTransformer
    embedder = SentenceTransformer(
        mmlw_info["repo_id"],
        revision=mmlw_info["revision"],
        local_files_only=True,
    )
    emb = embedder.encode("Offline embedding test")
    assert emb is not None
    assert emb.shape == (768,)

    # Test ładowania offline CrossEncoder
    reranker = CrossEncoder(
        reranker_info["repo_id"],
        revision=reranker_info["revision"],
        local_files_only=True,
    )
    scores = reranker.predict([("Pytanie testowe", "Odpowiedź testowa")])
    assert scores is not None
    assert len(scores) == 1

