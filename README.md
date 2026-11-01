# School Textbooks RAG v2

A multilingual AI-powered RAG platform for school textbooks covering primary, preparatory, and secondary education.

The platform supports Arabic, English, and French, with Vision Language Models (VLMs) for page understanding, Multi-RAG subject routing, hybrid retrieval, and isolated chat sessions for individual students.

Built with FastAPI, PostgreSQL, Qdrant, Redis, Celery, and configurable AI providers.

---

## Table of Contents

- [Overview](#overview)
- [Key Features](#key-features)
- [Architecture](#architecture)
- [Project Structure](#project-structure)
- [Supported Providers](#supported-providers)
- [Technology Stack](#technology-stack)
- [Prerequisites](#prerequisites)
- [Configuration](#configuration)
- [Running with Docker](#running-with-docker)
- [Creating the First Admin](#creating-the-first-admin)
- [API Usage](#api-usage)
- [Document Ingestion Pipeline](#document-ingestion-pipeline)
- [Retrieval and Answer Generation](#retrieval-and-answer-generation)
- [Database Migrations](#database-migrations)
- [Running Tests](#running-tests)
- [Production Deployment](#production-deployment)
- [Security](#security)
- [Known Limitations](#known-limitations)

---

## Overview

School Textbooks RAG v2 is a backend platform that allows students to ask questions about their school textbooks and receive answers grounded in the uploaded educational material.

The system supports three educational stages:

- Primary: Grades 1–6
- Preparatory: Grades 1–3
- Secondary: Grades 1–3

Students can interact with the platform in Arabic, English, or French.

Each student has independent chat sessions, and retrieval is restricted by the student's authorized grade and other server-side filters.

The project follows a layered architecture with configurable providers, allowing AI models and infrastructure components to be replaced without coupling business logic to specific SDKs.

---

## Key Features

### Multilingual Document Intelligence

- Arabic, English, and French support.
- PDF document ingestion.
- Native PDF text extraction.
- VLM-based page understanding.
- OCR fallback for scanned pages.
- Markdown and LaTeX-oriented content extraction.
- Extraction of information from tables, diagrams, and mathematical content.

### Retrieval-Augmented Generation

- Structure-aware text chunking.
- Multilingual embeddings using BGE-M3.
- Dense and sparse hybrid retrieval when supported by the embedding provider.
- Metadata-based filtering.
- Grade, subject, and book filtering.
- Optional reranking.
- Context-aware follow-up question rewriting.
- Source citations containing book and page information.

### Multi-RAG

- Subject-aware question routing.
- Retrieval across relevant subjects.
- Configurable routing confidence and subject limits.
- Option to disable routing and search all subjects available to the student.

### Student Chat Management

- Independent chat sessions.
- Student-level conversation isolation.
- Conversation history for follow-up questions.
- Server-side authorization and grade restrictions.
- Streaming responses using Server-Sent Events (SSE).

### Background Processing

- Celery-based asynchronous document ingestion.
- Separate ingestion and maintenance workers.
- Retry support for failed pages.
- Resumable processing.
- Scheduled maintenance and recovery tasks.

### Reliability and Monitoring

- Health and readiness endpoints.
- Redis-based rate limiting and distributed locks.
- Per-student daily usage limits.
- Global request limits.
- Prometheus metrics.
- Grafana dashboards.
- Structured logging and centralized exception handling.

---

## Architecture

The application follows a layered architecture:

```text
                    HTTP Request
                         |
                         v
                       Router
                         |
                         v
                     Controller
                         |
                         v
                       Service
                      /   |   
                     /    |    
                    v     v     v
              Repository Providers Other Services
                    |
                    v
                 Database
```

### Application Layers

| Layer | Responsibility |
|---|---|
| Routers | HTTP endpoints and request routing |
| Controllers | Coordinate HTTP input/output with services |
| Services | Application and business logic |
| Repositories | Database access and queries |
| Models | Database entities |
| Schemas | Request and response validation |
| Providers | External AI and infrastructure integrations |
| Workers | Asynchronous background processing |
| Core | Configuration, security, database setup, and shared utilities |

### Provider Design

AI and infrastructure integrations are isolated behind provider interfaces.

Services depend on abstractions instead of directly depending on vendor SDKs.

A provider factory and registry select implementations based on environment configuration.

This makes it possible to switch supported providers without rewriting application services.

---

## Project Structure

```text
school-rag-v2/
|
|-- .github/
|   |-- workflows/
|       |-- ci.yml
|       |-- deploy.yml
|
|-- alembic/
|   |-- versions/
|   |-- env.py
|
|-- app/
|   |-- controllers/
|   |-- core/
|   |-- models/
|   |-- providers/
|   |   |-- embeddings/
|   |   |-- llm/
|   |   |-- ocr/
|   |   |-- rerankers/
|   |   |-- vectorstores/
|   |   |-- vlm/
|   |   |-- base.py
|   |   |-- factory.py
|   |
|   |-- repositories/
|   |-- routers/
|   |-- schemas/
|   |-- services/
|   |-- workers/
|   |
|   |-- cli.py
|   |-- main.py
|
|-- deploy/
|   |-- grafana/
|   |-- nginx/
|   |-- prometheus/
|   |-- backup.sh
|   |-- deploy.sh
|   |-- RUNBOOK.md
|
|-- docker/
|   |-- entrypoint.sh
|
|-- tests/
|
|-- .dockerignore
|-- .env.example
|-- .gitignore
|-- alembic.ini
|-- docker-compose.yml
|-- Dockerfile
|-- pytest.ini
|-- requirements.txt
|-- requirements-dev.txt
|-- README.md
```

---

## Supported Providers

The application supports independently configurable providers for different AI roles.

### LLM Providers

Used for generating student answers and rewriting follow-up questions.

| Provider | Supported |
|---|---|
| Groq | Yes |
| Gemini | Yes |
| Anthropic | Yes |
| OpenAI | Yes |

### VLM Providers

Used for understanding page images and extracting information from visual educational content.

| Provider | Supported |
|---|---|
| Groq | Yes |
| Gemini | Yes |
| Anthropic | Yes |
| OpenAI | Yes |

### Other Providers

| Component | Available Implementations |
|---|---|
| Embeddings | BGE-M3, OpenAI |
| Reranking | BGE Reranker, No-op |
| Vector Store | Qdrant |
| OCR | Tesseract, No-op |

The OpenAI-compatible configuration can also be used with compatible API servers when the selected provider and base URL support the required API format.

### Changing Providers

Provider selection is controlled through environment variables.

For example:

```env
LLM_PROVIDER=groq
LLM_MODEL=meta-llama/llama-4-scout-17b-16e-instruct

CONDENSE_PROVIDER=groq
CONDENSE_MODEL=llama-3.1-8b-instant

VLM_PROVIDER=groq
VLM_MODEL=meta-llama/llama-4-scout-17b-16e-instruct

EMBED_PROVIDER=bge_m3
EMBED_MODEL=BAAI/bge-m3
EMBED_DIM=1024

RERANK_PROVIDER=bge
RERANK_MODEL=BAAI/bge-reranker-v2-m3

VECTORSTORE_PROVIDER=qdrant
OCR_PROVIDER=tesseract
```

To inspect configured and available providers:

```powershell
docker compose exec api python -m app.cli providers
```

### Adding a New Provider

The provider architecture supports extension through the provider interfaces and registry.

Typical integration steps:

1. Implement the corresponding interface from `app/providers/base.py`.
2. Register the implementation in `app/providers/factory.py`.
3. Configure the new provider through the appropriate environment variable.

---

## Technology Stack

| Category | Technologies |
|---|---|
| Language | Python 3.11 |
| API Framework | FastAPI |
| API Documentation | Swagger UI / OpenAPI |
| Data Validation | Pydantic |
| Database | PostgreSQL 16 |
| ORM | SQLAlchemy Async |
| Database Migrations | Alembic |
| Vector Database | Qdrant |
| Cache and Rate Limiting | Redis |
| Background Tasks | Celery |
| Embeddings | BGE-M3 / OpenAI |
| LLM Integration | Groq, Gemini, Anthropic, OpenAI |
| Vision Models | Groq, Gemini, Anthropic, OpenAI |
| OCR | Tesseract |
| PDF Processing | PyMuPDF |
| Authentication | JWT |
| Password Hashing | Argon2 |
| Reverse Proxy | Nginx |
| Monitoring | Prometheus and Grafana |
| Containerization | Docker and Docker Compose |
| Testing | Pytest |
| CI/CD | GitHub Actions |

---

## Prerequisites

Before running the application, install:

- Git
- Docker Desktop
- Docker Compose
- A Groq API key for the default LLM and VLM configuration

Alternative API providers can be configured through their respective API keys.

The local BGE-M3 embedding and reranking models require additional disk space and memory.

The first model download may take some time.

---

## Configuration

### 1. Clone the Repository

```powershell
git clone https://github.com/YOUR_USERNAME/school-textbooks-rag.git

cd school-textbooks-rag
```

Replace `YOUR_USERNAME` with your GitHub username.

### 2. Create the Environment File

```powershell
Copy-Item .env.example .env
```

### 3. Configure Environment Variables

Open `.env` and configure the required values.

Important settings:

| Variable | Description |
|---|---|
| APP_ENV | Application environment |
| JWT_SECRET | Secret used for JWT signing |
| CORS_ORIGINS | Allowed origins |
| GROQ_API_KEY | Groq API key |
| GEMINI_API_KEY | Optional Gemini API key |
| ANTHROPIC_API_KEY | Optional Anthropic API key |
| OPENAI_API_KEY | Optional OpenAI API key |
| POSTGRES_PASSWORD | PostgreSQL password |
| FLOWER_PASSWORD | Celery Flower password |
| GRAFANA_PASSWORD | Grafana administrator password |

The default configuration uses Groq for LLM and VLM, local BGE-M3 embeddings and reranking, Qdrant, and Tesseract.

Replace all example passwords and secrets before running the application.

Never commit the actual `.env` file.

---

## Running with Docker

The recommended way to run the complete infrastructure is Docker Compose.

### 1. Build and Start Services

```powershell
docker compose up -d --build
```

The stack includes:

- API
- PostgreSQL
- Redis
- Qdrant
- Celery ingestion worker
- Celery maintenance worker
- Celery Beat
- Flower
- Nginx
- Prometheus
- Grafana
- Database and Redis exporters

### 2. Check Running Containers

```powershell
docker compose ps
```

### 3. View API Logs

```powershell
docker compose logs -f api
```

### 4. Access Swagger UI

```text
http://localhost:8000/docs
```

The API documentation is available through Swagger UI.

The first startup may take longer because local AI models need to be downloaded and initialized.

---

## Creating the First Admin

After the API becomes available, create the initial administrator:

```powershell
docker compose exec api python -m app.cli create-admin --email admin@school.com --password "ChangeMe-12345" --name "Admin"
```

Use a unique, strong password instead of the example password.

The Docker API entrypoint automatically applies Alembic migrations before starting the API server.

---

## API Usage

The application exposes versioned HTTP endpoints.

The main API capabilities include:

### Authentication

```text
POST /auth/login
```

Returns an authentication token.

Use the token through the Swagger Authorize feature.

### Book Management

```text
POST /admin/books
GET  /admin/books/{id}
POST /admin/books/{id}/retry
POST /admin/books/{id}/reindex
```

Administrators can upload books, inspect processing status, retry failed pages, and rebuild vector indexes.

### Student Management

```text
POST /admin/students
```

Administrators can create student accounts and configure student limits.

### Student Conversations

```text
POST /chat/sessions
POST /chat/sessions/{id}/ask
```

Students can create independent chat sessions and ask questions about their authorized educational material.

The ask endpoint supports streaming responses using Server-Sent Events (SSE).

Typical event types include:

- `meta`
- `token`
- `citations`
- `done`
- `error`

Actual request and response schemas are documented in Swagger UI.

---

## Document Ingestion Pipeline

The ingestion pipeline processes uploaded educational PDFs asynchronously.

```text
PDF Upload
    |
    v
Document Validation
    |
    v
PDF Page Extraction
    |
    v
Native Text Extraction / VLM
    |
    v
OCR Fallback
    |
    v
Page Text Storage
    |
    v
Structure-Aware Chunking
    |
    v
Embedding Generation
    |
    v
Qdrant Indexing
```

### Processing Strategy

The system supports multiple extraction paths:

- Native PDF text extraction when suitable text is available.
- VLM-based extraction for visually complex pages.
- OCR fallback for pages that require optical character recognition.

Extracted page content is saved in PostgreSQL.

This supports resumable processing and reduces the need to repeat expensive VLM extraction.

### Failure Handling

- Failed pages can be retried.
- Successfully processed pages are preserved.
- A worker interruption does not necessarily require restarting the entire book.
- Processing failures are tracked at the page and book levels.

---

## Retrieval and Answer Generation

The student question pipeline follows this general flow:

```text
Student Question
      |
      v
JWT Authentication
      |
      v
Usage Limits and Rate Limiting
      |
      v
Follow-up Question Rewriting
      |
      v
Multi-RAG Subject Routing
      |
      v
Filtered Vector Search
      |
      v
Optional Reranking
      |
      v
LLM Answer Generation
      |
      v
SSE Response + Citations
      |
      v
Conversation Storage
```

### Retrieval Features

- Grade-aware retrieval.
- Subject-aware retrieval.
- Optional book filtering.
- Dense and sparse hybrid retrieval when supported.
- Optional BGE reranking.
- Context-aware follow-up questions.
- Book and page citations.

### Embedding Model Changes

Changing the embedding model or its dimensions creates a separate Qdrant collection.

Existing documents must be reindexed using:

```text
POST /admin/books/{id}/reindex
```

Reindexing uses previously stored page text, avoiding repeated VLM extraction.

---

## Database Migrations

The project uses Alembic for database schema management.

Migration files are located in:

```text
alembic/
    env.py
    versions/
        0001_initial.py
```

To apply migrations manually:

```powershell
docker compose exec api alembic upgrade head
```

To inspect the current migration:

```powershell
docker compose exec api alembic current
```

To view migration history:

```powershell
docker compose exec api alembic history
```

The Docker API entrypoint automatically executes:

```text
alembic upgrade head
```

before starting Uvicorn.

---

## Running Tests

Install development dependencies in a Python environment:

```powershell
python -m venv .venv

.\.venv\Scripts\Activate.ps1

python -m pip install -r requirements-dev.txt
```

Run the test suite:

```powershell
python -m pytest -q
```

Run Python compilation checks:

```powershell
python -m compileall -q app
```

Tests should be executed in an environment with the required dependencies and database configuration.

Passing local tests does not automatically establish that every external provider or the complete production Docker stack works end-to-end.

---

## Production Deployment

The repository includes a production-oriented Docker Compose stack.

### Reverse Proxy

Nginx acts as the public entry point.

The API and infrastructure services are separated from direct public access.

### Background Processing

Celery workers handle document ingestion and maintenance tasks.

Celery Beat schedules periodic tasks.

Flower provides a monitoring interface for Celery.

### Monitoring

Prometheus collects application and infrastructure metrics.

Grafana provides dashboards for monitoring.

Administrative monitoring interfaces are bound to localhost in the provided Compose configuration.

Refer to:

```text
deploy/RUNBOOK.md
```

for deployment, backup, and operational instructions.

### Important Data Safety Warning

Do not run:

```powershell
docker compose down -v
```

unless you intentionally want to delete the persistent Docker volumes.

This command can remove PostgreSQL data, Qdrant vectors, uploaded storage, and other persistent application data.

---

## Security

Security-related features include:

- JWT-based authentication.
- Argon2 password hashing.
- Student-level data isolation.
- Server-side grade filtering.
- Rate limiting.
- Daily usage limits.
- Distributed chat locks.
- Environment-based secrets.
- Production configuration validation.
- Restricted monitoring service exposure.

Before public or production deployment:

1. Generate a strong random JWT secret.
2. Replace all default infrastructure passwords.
3. Configure explicit CORS origins.
4. Keep API keys outside source control.
5. Use HTTPS.
6. Review server firewall rules.
7. Restrict administrative access.
8. Use only educational material that you have permission to upload and distribute.

Never publish real student information, credentials, API keys, or private educational documents.

---

## Known Limitations

This project is designed as a production-oriented backend, but production readiness must be validated in the target environment.

Important limitations:

- External LLM and VLM integrations require valid provider credentials.
- Local BGE models require additional memory and initial downloads.
- Actual performance depends on hardware, document complexity, and provider limits.
- External API integrations should be tested with real credentials before deployment.
- The complete Docker stack should be validated in the intended hosting environment.
- No frontend is included; Swagger UI is used for API exploration and testing.
- End-to-end behavior should be validated with representative educational PDFs before real student use.

---

## CI/CD

GitHub Actions workflows are provided under:

```text
.github/workflows/
```

The CI workflow includes:

- Python setup.
- Dependency installation.
- Python compilation checks.
- Automated tests.
- Alembic migration validation.
- Docker Compose configuration validation.

Deployment automation is also included and should be configured with the appropriate server credentials and GitHub repository secrets before use.

---

## License

No open-source license has been specified yet.

All rights to third-party models, libraries, datasets, and educational materials remain subject to their respective licenses and terms.

---

## Project Goal

The goal of School Textbooks RAG v2 is to provide a modular and extensible AI-powered educational assistant that helps students understand their school textbooks through multilingual document intelligence, reliable retrieval, and grounded conversational answers.
