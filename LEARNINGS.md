# LEARNINGS — SkillGap AI Coach

## 1. What I Attempted

The goal of this app is to **compare a resume to a job description**, compute a **skill match score** (0–100), list **overlapping** and **missing skills**, and suggest **next steps** to improve fit. It is a **full-stack app**: Next.js (frontend) + FastAPI (backend) + Postgres (persistence) + Docker (orchestration), with an **optional LLM mode** (OpenAI) for richer suggestions when an API key is configured.

---

## 2. Architecture Snapshot

**Frontend (Next.js, React + TypeScript)**  
- Single main page (`frontend/src/app/page.tsx`) with two text areas (resume, job description), an "Analyze" button, and a results panel.
- The client calls the backend `POST /analyze` with `{ resume_text, job_description }` via `frontend/src/lib/api.ts`.
- The UI renders: match score (with color bands), overlapping skills (with evidence snippets), missing skills, and suggested next steps. A sidebar shows **Recent History** (last 10 runs); each item can be clicked to re-populate inputs and results, or deleted. History is loaded on mount and after each analysis via `GET /history`; clear-all and delete-one use `DELETE /history` and `DELETE /history/{id}`.

**Backend (FastAPI)**  
- `main.py` defines the API: `POST /analyze`, `GET /history`, `DELETE /history`, `DELETE /history/{id}`. Request/response shapes are Pydantic models (`AnalyzeRequest`, `AnalyzeResponse`, `HistoryItem`).
- **Baseline logic** lives in `services/baseline.py`: skill extraction via a deterministic keyword dictionary (`services/skill_dict.py`), text normalization and tokenization (words + bigrams), match score as percentage of job skills found in the resume, evidence snippets per overlapping skill, and a fixed bullet-list for suggested next steps. No external APIs.
- **Optional LLM path** is in `services/llm_service.py`: it runs the baseline first, then optionally calls OpenAI to generate suggested next steps; score and skill lists stay from the baseline. If the key is missing or the API fails, it falls back to the baseline result.
- **Orchestration** is in `services/analyzer.py`: it checks `USE_LLM_MODE` and `OPENAI_API_KEY` and calls either `run_llm_analysis` or `run_baseline_analysis`. Domain types are in `services/models.py` (`AnalysisResult`, `SkillWithEvidence`).

**Database (Postgres)**  
- Used for **history** only. Schema in `db/schema.py`: `AnalysisRun` with `id`, `timestamp`, `resume_summary`, `job_title_guess`, `match_score`, and `result_json` (JSONB). Each successful `/analyze` is stored unless it is an exact consecutive duplicate of the previous run (same resume + job text). `GET /history` returns the last 10 runs with full `resume_text`, `job_description`, and `result` so the frontend can reload any past run.

**Docker**  
- `docker-compose.yml` defines three services: **db** (Postgres 16), **backend** (FastAPI), **frontend** (Next.js). Backend gets `DATABASE_URL`, `OPENAI_API_KEY`, and `USE_LLM_MODE`; frontend gets `NEXT_PUBLIC_API_URL`. Backend and frontend depend on db and backend healthchecks so startup order is correct. One command (`docker compose up`) runs the full stack.

---

## 3. What Broke / Got Tricky

- **Normalizing skills:** Case-insensitivity is handled by `normalize_text()` (lowercase, strip, replace punctuation with spaces) and by comparing sets in lowercase (e.g. `set(s.lower() for s in resume_skills)`). Pluralization and variants (e.g. "Kubernetes" vs "K8s") are handled by listing both in `SKILL_KEYWORDS` / `SKILL_WORDS` in `skill_dict.py`; there is no automatic stemming, so adding new variants means updating the dictionary. Duplicates are removed via sets and output is sorted for stable display.

- **Avoiding false positives:** Matching is **dictionary-only**: tokens (and bigrams) are extracted from normalized text and only counted as skills if they appear in `SKILL_WORDS` or `SKILL_KEYWORDS`. That avoids substring matches (e.g. "c" or "sql" inside unrelated words). Phrases like "machine learning" are matched only when the full bigram appears in the normalized text, so the design is testable and predictable.

- **JSON contract between frontend and backend:** The request/response shapes had to stay in sync: `resume_text` / `job_description` in the body; response with `match_score`, `overlapping_skills` (each with `skill` and `evidence`), `missing_skills`, `suggested_next_steps`, and `mode`. The frontend types in `frontend/src/types/index.ts` mirror the backend (`AnalysisResponse`, `HistoryItem`). Error handling uses a consistent `detail` string in JSON on 4xx/5xx so the client can show it.

- **Optional LLM mode:** The app must work without any API key. The analyzer only calls the LLM path when `USE_LLM_MODE` is true and `OPENAI_API_KEY` is set. The LLM service runs the baseline first and catches all exceptions; on failure it returns the baseline result so the user always gets a valid response.

- **Coordinating three services in Docker:** Ports (5432, 8000, 3000), env vars (e.g. `DATABASE_URL` with host `db`), and healthchecks so the backend waits for Postgres and the frontend waits for the backend. CORS is configured in the backend for the frontend origin.

---

## 4. What I Learned (Technical Delta)

- **API contract design:** Defining a single, clear request/response shape (Pydantic on the backend, TypeScript interfaces on the frontend) and using it for both the live API and for storing `result` in history avoids drift and makes the frontend’s "reload from history" behavior straightforward.

- **Deterministic baseline first:** Having a baseline algorithm that does not depend on external APIs means the app always works without keys, is easy to test (e.g. `test_baseline.py`, `test_analyze.py`), and gives consistent scores. The LLM is used only to enrich one part (suggested next steps) while reusing the same score and skill lists.

- **Structuring skill extraction and scoring:** Keeping extraction, scoring, and suggestion in `services/baseline.py` (and the dictionary in `skill_dict.py`) separates "what we detect" from the HTTP layer. The API just calls `analyze_resume_vs_job()` and maps the result to JSON; all edge cases (empty text, no job skills, evidence snippets) are testable without the UI.

- **Separation of concerns:** Baseline handles scoring and skill lists; the LLM service only replaces `suggested_next_steps` and sets `mode="llm"`. That keeps behavior predictable and makes it easy to add or change LLM behavior without touching the core scoring logic.

---

## 5. Why This Version Is Better

- **Clear separation between baseline and LLM:** Baseline logic lives in `baseline.py`, LLM in `llm_service.py`, and the choice is centralized in `analyzer.py`. Config is read from `config.py` (e.g. `use_llm_mode`).

- **Controlled matching and normalization:** Skills are matched only from a fixed dictionary after tokenization (words + bigrams), with lowercase comparison and set operations to remove duplicates. Evidence is produced per overlapping skill via sentence or context snippets in `_find_evidence_for_skill`.

- **History in Postgres:** Analyses are stored in `AnalysisRun` with a JSONB payload, so history survives restarts and is shared across clients. Consecutive duplicate runs are not stored to avoid clutter. The frontend can reload any of the last 10 runs and delete individual items or clear all.

- **One-command local dev:** `docker compose up` brings up db, backend, and frontend with the right env and dependencies; no need to start Postgres, backend, and frontend manually.

---

## 6. Next Iteration Plan

- **Richer skill ontology:** Synonyms, multi-word variants, or embeddings (e.g. pgvector) to match skills that are phrased differently in the job vs the resume.

- **Authentication:** Optional login so users can save multiple resumes/jobs and associate history with accounts.

- **Export:** Download the analysis report as PDF or markdown (score, overlapping/missing skills, next steps).

- **Analytics:** Track match scores over time (e.g. per user or per job) to see improvement trends.
