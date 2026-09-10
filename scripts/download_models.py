#!/usr/bin/env python3
"""
scripts/download_models.py
Dedykowany skrypt do pobierania i kryptograficznej weryfikacji sum kontrolnych SHA-256
dla polskich modeli NLP (Embeddingi MMLW oraz Reranker RoBERTa-v3) w trybie Offline/CI/Docker.
"""

import sys
import os
import argparse
import hashlib
import logging
from pathlib import Path
from typing import Dict, Any, Tuple, List, Optional

# Zabezpieczenie konsoli Windows przed UnicodeEncodeError
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("download_models")

# ==============================================================================
# Manifest modeli ze ścisłymi sumami kontrolnymi SHA-256
# ==============================================================================
MODEL_MANIFEST: Dict[str, Dict[str, Any]] = {
    "sdadas/mmlw-e5-base": {
        "repo_id": "sdadas/mmlw-e5-base",
        "revision": "9ba15feff8a15b0bb1433a49e10d64de2a1fe772",
        "description": "Polski model embeddingów zdań (MMLW e5-base, 768d)",
        "files": {
            "model.safetensors": "f50cea47c8eb01dc5e84a45e72e4fef90fb9513e8e56979b6b9a6ed1ae023ecd",
            "config.json": "cd799d44087d2e93140dbd0977f2ed9659fed874e1b61f019b96158fcdd2caeb",
            "tokenizer.json": "46afe88da5fd71bdbab5cfab5e84c1adce59c246ea5f9341bbecef061891d0a7",
            "tokenizer_config.json": "efb5c0d09722e5fe59a462cd2a9976ee216d55b037597d997cd3fe833216da15",
            "special_tokens_map.json": "06e405a36dfe4b9604f484f6a1e619af1a7f7d09e34a8555eb0b77b66318067f",
            "sentencepiece.bpe.model": "cfc8146abe2a0488e9e2a0c56de7952f7c11ab059eca145a0a727afce0db2865",
            "modules.json": "8f4b264b80206c830bebbdcae377e137925650a433b689343a63bdc9b3145460",
            "config_sentence_transformers.json": "ad36309aa7927231717487b28f198d0bb55cbd5da37eeaae76ff8f49c06ce463",
            "sentence_bert_config.json": "ec8e29d6dcb61b611b7d3fdd2982c4524e6ad985959fa7194eacfb655a8d0d51",
            "1_Pooling/config.json": "c9bef85e8bbf4b2eab4941b3fb62bd33f88686748b478f2e264d256472d9643b",
        },
    },
    "sdadas/polish-reranker-roberta-v3": {
        "repo_id": "sdadas/polish-reranker-roberta-v3",
        "revision": "e6471da541f4e7be33845b6d57248a8d8bde27e8",
        "description": "Polski Cross-Encoder reranker oparty na RoBERTa v3",
        "files": {
            "model.safetensors": "3043a8d96704ebec7dc9921ba4bc97dee6ba24b9ced9c8ca9813b79e8ec2b535",
            "config.json": "9b6f62df65b527f7323e16e7135e6df0887a43ca8de4de452ab1f60d0589e648",
            "tokenizer.json": "5220e6ece9473a6e3ccd333b33e75d81a9487c90d6da13bfb4b23b076cd4b239",
            "tokenizer_config.json": "c6d6a594c30c89ee1470f6e5d1889819831216d5ff2e12806d1ee8c35e1f114b",
            "special_tokens_map.json": "8c785abebea9ae3257b61681b4e6fd8365ceafde980c21970d001e834cf10835",
            "added_tokens.json": "cb4917fcffe82e131fb04d3c52eabfda89d3690ef972cca09b981d610e28bf19",
        },
    },
}


def compute_sha256(file_path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Oblicza sumę kontrolną SHA-256 dla wskazanego pliku w blokach 1MB."""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(chunk_size):
            hasher.update(chunk)
    return hasher.hexdigest()


def find_snapshot_dir(repo_id: str, revision: str, cache_dir: Optional[Path] = None) -> Optional[Path]:
    """Wyszukuje katalog snapshotu modelu w lokalnym cache HuggingFace."""
    if cache_dir is None:
        from huggingface_hub import constants
        cache_dir = Path(constants.HF_HUB_CACHE)

    repo_folder_name = "models--" + repo_id.replace("/", "--")
    repo_dir = cache_dir / repo_folder_name
    snapshot_dir = repo_dir / "snapshots" / revision

    if snapshot_dir.exists() and snapshot_dir.is_dir():
        return snapshot_dir

    # Jeśli podano bezpośredni katalog zawierający wagi
    if (cache_dir / "model.safetensors").exists():
        return cache_dir

    return None


def verify_model_files(model_dir: Path, expected_files: Dict[str, str]) -> Tuple[bool, List[str]]:
    """Weryfikuje istnienie i sumy kontrolne plików modelu."""
    errors = []
    for filename, expected_hash in expected_files.items():
        file_path = model_dir / filename
        if not file_path.exists():
            errors.append(f"Brak pliku: {filename}")
            continue

        # Rozwiązanie ewentualnych linków symbolicznych w cache HF
        real_path = file_path.resolve()
        actual_hash = compute_sha256(real_path)
        if actual_hash.lower() != expected_hash.lower():
            errors.append(
                f"Niezgodność SHA-256 dla {filename}: oczekiwano {expected_hash}, otrzymano {actual_hash}"
            )

    return len(errors) == 0, errors


def download_or_verify_model(
    model_key: str,
    cache_dir: Optional[Path] = None,
    verify_only: bool = False,
    force: bool = False,
) -> bool:
    """Pobiera model z HuggingFace Hub (jeśli brak) i weryfikuje sumy kontrolne SHA-256."""
    if model_key not in MODEL_MANIFEST:
        logger.error(f"Nieznany model: {model_key}. Dostępne w manifeście: {list(MODEL_MANIFEST.keys())}")
        return False

    info = MODEL_MANIFEST[model_key]
    repo_id = info["repo_id"]
    revision = info["revision"]
    expected_files = info["files"]

    logger.info(f"--- Przetwarzanie modelu: {model_key} ---")
    logger.info(f"Opis: {info['description']}")
    logger.info(f"Repozytorium: {repo_id} (rewizja: {revision[:8]}...)")

    snapshot_dir = find_snapshot_dir(repo_id, revision, cache_dir)

    if snapshot_dir and not force:
        logger.info(f"Znaleziono istniejący snapshot w: {snapshot_dir}")
        logger.info("Weryfikacja sum kontrolnych SHA-256...")
        valid, errors = verify_model_files(snapshot_dir, expected_files)
        if valid:
            logger.info(f"✅ Model {model_key} jest poprawny i kompletny!")
            return True
        else:
            for err in errors:
                logger.warning(f"  ⚠️ {err}")
            if verify_only:
                return False
            logger.info("Wykryto uszkodzone lub brakujące pliki. Ponawianie pobierania...")

    if verify_only:
        logger.error(f"❌ Model {model_key} nie jest zainstalowany w cache (tryb verify-only).")
        return False

    # Pobieranie modelu za pomocą huggingface_hub
    logger.info(f"Pobieranie {repo_id}@{revision}...")
    try:
        from huggingface_hub import snapshot_download

        downloaded_dir = Path(
            snapshot_download(
                repo_id=repo_id,
                revision=revision,
                cache_dir=cache_dir,
                force_download=force,
            )
        )
        logger.info(f"Pobrano do: {downloaded_dir}")
        logger.info("Weryfikacja sum kontrolnych pobranego modelu...")
        valid, errors = verify_model_files(downloaded_dir, expected_files)
        if valid:
            logger.info(f"✅ Model {model_key} pomyślnie pobrany i zweryfikowany!")
            return True
        else:
            for err in errors:
                logger.error(f"  ❌ {err}")
            return False
    except Exception as e:
        logger.error(f"Błąd podczas pobierania {model_key}: {e}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pobieranie i weryfikacja SHA-256 modeli NLP dla KARIK RAG"
    )
    parser.add_argument(
        "--models",
        nargs="+",
        choices=list(MODEL_MANIFEST.keys()),
        default=list(MODEL_MANIFEST.keys()),
        help="Modele do pobrania/weryfikacji (domyślnie wszystkie)",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Katalog cache HuggingFace (domyślnie systemowy cache HF)",
    )
    parser.add_argument(
        "--verify-only",
        "--check",
        action="store_true",
        help="Tylko sprawdź sumy kontrolne bez pobierania z sieci",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Wymuś ponowne pobranie nawet jeśli pliki istnieją",
    )

    args = parser.parse_args()

    success_count = 0
    total = len(args.models)

    for model_key in args.models:
        ok = download_or_verify_model(
            model_key=model_key,
            cache_dir=args.cache_dir,
            verify_only=args.verify_only,
            force=args.force,
        )
        if ok:
            success_count += 1

    logger.info("=" * 60)
    logger.info(f"Wynik: {success_count}/{total} modeli zweryfikowanych pomyślnie.")
    logger.info("=" * 60)

    return 0 if success_count == total else 1


if __name__ == "__main__":
    sys.exit(main())
