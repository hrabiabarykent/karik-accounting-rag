# KARIK – Hybrid Accounting & Tax Law RAG System (Polish Jurisdiction)

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.111%2B-009688.svg)](https://fastapi.tiangolo.com)
[![Celery](https://img.shields.io/badge/Celery-5.4-brightgreen.svg)](https://docs.celeryq.dev/)
[![PostgreSQL pgvector](https://img.shields.io/badge/PostgreSQL-16%20%2B%20pgvector-336791.svg)](https://github.com/pgvector/pgvector)
[![Pytest](https://img.shields.io/badge/Tests-22%20Passed-success.svg)](https://docs.pytest.org/)
[![Jurisdiction: Poland](https://img.shields.io/badge/Jurisdiction-Poland%20%F0%9F%87%B5%F0%9F%87%B1-dc2626.svg)](https://isap.sejm.gov.pl/)
[![License: Source-Available](https://img.shields.io/badge/License-Source--Available-amber.svg)](LICENSE)

> 🇵🇱 **Dedicated to the Polish Tax & Accounting Legal System**: Purpose-built for the Polish jurisdiction, compliant with Polish GAAP (*Ustawa o Rachunkowości*), corporate & personal income taxes (CIT / PIT), goods and services tax (VAT), social security system (ZUS), the Polish Tax Code (*Ordynacja Podatkowa*), and native parsing of the Polish National e-Invoicing System (**KSeF XML**).

An asynchronous, hybrid accounting document processing system (PDF, PNG, JPEG, KSeF XML) combining a **local GDPR/PII privacy layer (Zero-Trust Edge)** with an **Advanced RAG engine tailored for Polish tax and fiscal legislation** and an automated financial reconciliation audit loop.

---

## 📐 System Architecture

```mermaid
graph TD
    subgraph Input [Input: Polish Accounting Documents & Queries]
        A[Invoice: PDF / JPG / PNG / KSeF XML FA_2]
        Q[Polish Tax / Accounting Legal Query]
    end

    subgraph GDPR / PII Anonymization Layer (Local Edge)
        A --> B[Parser: KSeF XML / PDF / Vision OCR]
        B --> C[Presidio + Polish Checksums NIP/PESEL/REGON/IBAN/KSeF]
        C --> D[Local Gemma 4 SLM / llama.cpp CUDA]
        D --> E[Mapping Dictionary in Redis RAM]
        D --> F[Anonymized Text]
    end

    subgraph Advanced RAG Engine (Polish Statutory Acts)
        Q --> G[Polish Legal Query Expansion / HyDE]
        G --> H[PostgreSQL pgvector: HNSW + Polish FTS RRF]
        H --> I[Polish Cross-Encoder Reranker: roberta-v3]
        I --> J[Gemini Context Caching: 30 min TTL]
    end

    subgraph Synthesis & Audit
        F --> K[Google Gemini API: Polish Chart of Accounts & JSON Entry]
        J --> L[Google Gemini API: Grounded Legal Response with Article Citations]
        K --> M[Fault-Tolerant Detokenization in RAM]
        M --> N[Model-as-an-Auditor Reconciliation Loop]
        N --> O[Status: VERIFIED / REQUIRES_MANUAL_VERIFICATION]
    end
```

---

## 🛠️ Technology Stack

| Layer | Technologies | Role & Purpose |
| :--- | :--- | :--- |
| **API & Gateway** | FastAPI, Uvicorn, Pydantic v2 | Asynchronous REST entrypoint and schema validation |
| **Task Queue & Broker** | Celery, Redis 7.2 | Background processing of heavy document workflows |
| **GDPR Privacy (Edge AI)** | Microsoft Presidio, spaCy (`pl_core_news_lg`), `llama.cpp` CUDA (`gemma-4-E4B-it`) | Local PII & sensitive business data masking before cloud dispatch |
| **Vector DB & RAG** | PostgreSQL 16 with `pgvector` (HNSW) + Full-Text Search (`tsvector`) | Hybrid retrieval across Polish statutory acts with Reciprocal Rank Fusion (RRF $k=60$) |
| **NLP Models (SOTA Polish)** | `sdadas/mmlw-e5-base` (Embeddings), `sdadas/polish-reranker-roberta-v3` (Reranker) | High-precision semantic search and legal document re-ranking in Polish |
| **Cloud LLM** | Google Gemini API (`google.genai`), Context Caching | High-level statutory synthesis and Polish standard chart-of-accounts (*Plan Kont*) entries |
| **Testing & UI** | Pytest (22 tests), Streamlit (*Nordic Legal Emerald Theme*), HTML5/JS UI | Automated unit/integration test suite and interactive user interfaces |

---

## 🔑 Key Modules (Polish Fiscal & Legal Focus)

### 1. Polish GDPR / Zero-Trust Privacy Layer
* **Contextual Token Replacement**: Sensitive Polish entities (Tax ID / **NIP**, National Identification Number / **PESEL**, National Business Registry / **REGON**, Polish **IBAN** bank accounts, company names, addresses, emails, phone numbers, **KSeF identifiers**) are replaced with contextual placeholders (e.g. `<COMPANY_NAME_1>`, `<PL_NIP_1>`) before sending payload to cloud APIs.
* **Deterministic Checksum Validation**: Mathematical algorithmic validation of check digits specifically for Polish **NIP** (modulo 11), **PESEL** (weights 1, 3, 7, 9...), **REGON** (9- and 14-digit), and Polish **IBAN** (`PL` + 26 digits, modulo 97).
* **Native Polish KSeF XML Extraction**: Direct structural parsing for official Polish National e-Invoice XML schemas (**FA_VAT / FA_2**), bypassing OCR error rates and drastically reducing processing latency.
* **Fault-Tolerant Detokenization**: Memory-safe algorithm restoring original data from Redis, resilient to LLM formatting corruptions (e.g. spacing anomalies, altered casing).

### 2. Advanced RAG Engine for Polish Tax Law
* **Article-Level Chunking of Polish Acts**: Structure-aware law parser for major Polish tax and commercial codes, sourced directly from the official Sejm ISAP ELI database:
  * **PIT** (*Ustawa o podatku dochodowym od osób fizycznych*)
  * **CIT** (*Ustawa o podatku dochodowym od osób prawnych*)
  * **VAT** (*Ustawa o podatku od towarów i usług*)
  * **Ordynacja Podatkowa** (Polish General Tax Code)
  * **UoR** (*Ustawa o rachunkowości* / Polish GAAP)
  * **ZUS** (*Ustawa o systemie ubezpieczeń społecznych* / Social Security System)
  * **Prawo Przedsiębiorców** (Polish Entrepreneurs' Law / *Ulga na start*)
* **Hybrid Search (SQL RRF)**: Combines dense HNSW cosine similarity with Polish lexical Full-Text Search (`tsvector`) using Reciprocal Rank Fusion ($k=60$).
* **Polish Cross-Encoder Re-ranking**: Second-stage scoring with `sdadas/polish-reranker-roberta-v3` normalized via Sigmoid, fine-tuned for Polish syntax and legal nuances.
* **Context Caching**: Utilizing Google Gemini Context Caching for large statutory texts (30-minute TTL).

### 3. Model-as-an-Auditor Reconciliation Loop
* Secondary local verification pass where a local vision SLM cross-checks the original document image against generated JSON accounting entries (VAT rates: 23%, 8%, 5%, 0%, zw; GTU classification codes; debit/credit accounts: *Konto Wn / Ma*) to detect arithmetic discrepancies and missing items.

---

## 📊 RAG Benchmark Results (Evaluation on Polish Tax Law)

Quality evaluation benchmarks measured on a dataset of 15 complex Polish tax law scenarios ([`eval_results.json`](eval_results.json)):

| Metric | Result | Description |
| :--- | :---: | :--- |
| **Hit Rate @ 1** | **73.3%** | Accuracy of retrieving the exact ground-truth statutory article at rank #1 |
| **Hit Rate @ 3** | **73.3%** | Accuracy of retrieving the ground-truth statutory article within top 3 |
| **MRR (Mean Reciprocal Rank)** | **0.733** | Mean reciprocal rank of the first relevant legal article |
| **Faithfulness Score** | **66.2%** | Factual consistency and adherence to Polish statutory source context |

---

## 📁 Project Structure

```text
KARIK/
├── app.py                      # Streamlit interactive application (Nordic Legal Emerald Theme)
├── main.py                     # FastAPI Gateway & REST endpoints
├── tasks.py                    # Celery Worker (Async processing pipeline & LangFuse tracing)
├── pii_sanitizer.py            # Presidio + Polish NIP/PESEL/REGON/IBAN validators + Gemma SLM
├── eval_rag.py                 # RAG evaluation benchmark suite for Polish tax law
├── docker-compose.yml          # Multi-container setup (FastAPI, Worker, Redis, Postgres, llama.cpp)
├── Dockerfile                  # Production container image (Python 3.10-slim)
├── requirements.txt            # Python dependencies
├── LICENSE                     # Source-Available (Portfolio Review Only) License
├── prompts/                    # YAML prompt templates (Polish accounting & audit rules)
│   ├── prompt_step_a.yaml
│   ├── prompt_step_b.yaml
│   └── prompt_step_d.yaml
├── rag/                        # Advanced RAG core package
│   ├── db.py                   # PostgreSQL pgvector hybrid search (HNSW + FTS RRF)
│   ├── retriever.py            # Polish sentence-transformers embedding & Cross-Encoder re-ranking
│   ├── pipeline.py             # RAG synthesis & statutory citation builder
│   ├── query_rewriter.py       # Polish legal query expansion (Gemma SLM HyDE)
│   ├── context_cache.py        # Gemini Cloud Context Caching
│   └── parser.py               # Polish Sejm ELI HTML statutory acts parser
├── scripts/                    # Utility & ingestion scripts
│   ├── demo_anonymization_cli.py # CLI tester for local GDPR anonymization & detokenization
│   ├── ingest_laws.py          # Vector embedding generation & pgvector ingestion
│   ├── sync_static_laws.py     # HTML static legal library synchronizer
│   └── verify_laws_db.py       # Vector database integrity & deduplication auditor
└── tests/                      # Automated Pytest test suite (22 tests)
```

---

## 🛠️ Quick Start

### 1. Prerequisites
* Docker & Docker Compose
* NVIDIA GPU & NVIDIA Container Toolkit (for CUDA acceleration in `llama.cpp`)
* Python 3.10+ (for local host execution)

### 2. Environment Configuration
Create a `.env` configuration file from the template:
```bash
cp .env.example .env
```
Provide your Google Gemini API key:
```env
GEMINI_API_KEY=your_gemini_api_key_here
```

### 3. Local Model Weights
Place the quantized GGUF weights in the `./models` directory:
* `models/gemma-4-E4B-it-Q4_K_M.gguf`
* `models/mmproj-F16.gguf`

*(Weights can be downloaded from HuggingFace, e.g. `bartowski/gemma-4-E4B-it-GGUF`)*.

### 4. Run via Docker Compose
```bash
docker-compose up --build -d
```
The FastAPI backend and interactive UI will be available at: `http://localhost:8000/ui`.

### 5. Local Launch on Windows (1-Click)
For Windows environments, run the automated starter script:
```cmd
start_karik.bat
```

---

## 🧪 Automated Testing & Benchmarking

Run the complete 22-test Pytest unit and integration suite:
```bash
pytest tests/ -v
```

Execute the Polish tax law RAG evaluation benchmark:
```bash
python eval_rag.py
```

---

## 📄 License

This project is distributed under the **Source-Available (Portfolio Review Only)** license. The source code is publicly accessible for evaluation, educational, and recruitment review purposes only. See [LICENSE](LICENSE) for details.
