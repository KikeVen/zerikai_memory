"""
zerikai_memory — Local Synthesis Benchmark
Compares mistral:7b vs ornith:9b on real synthesis tasks.

Mirrors the exact prompt structure used by _query_ollama in main.py:
  - System: stable role + project brief (for KV cache parity)
  - User: ChromaDB context chunks + query

Usage:
    python benchmark_synthesis.py
    python benchmark_synthesis.py --models mistral:7b ornith:9b
    python benchmark_synthesis.py --samples 5 --host http://localhost:11434
"""

import argparse
import json
import statistics
import sys
import textwrap
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODELS = ["mistral:7b", "ornith:9b"]
DEFAULT_SAMPLES = 3  # runs per model per query

# ---------------------------------------------------------------------------
# Sample data — mirrors real ChromaDB entity payloads from zerikai_memory
# Each entry represents one ChromaDB document as returned by collection.get()
# with metadata fields that _query_ollama assembles into the user prompt.
# ---------------------------------------------------------------------------

CHROMADB_CONTEXT_SAMPLES = [
    {
        "query": "How does query routing decide between Ollama and DeepSeek?",
        "entities": [
            {
                "file": "main.py",
                "lines": "986-1010",
                "category": "function",
                "name": "_should_use_cloud",
                "content": textwrap.dedent("""\
                    def _should_use_cloud(user_query: str, use_cloud: Optional[bool] = None) -> bool:
                        # Priority 1: explicit override
                        if use_cloud is not None:
                            return use_cloud
                        # Priority 2: keyword match
                        q_lower = user_query.lower()
                        if any(kw in q_lower for kw in CLOUD_ESCALATION_KEYWORDS):
                            return True
                        # Priority 3: word count threshold
                        if len(user_query.split()) > CLOUD_ESCALATION_WORD_COUNT:
                            return True
                        # Priority 4: default mode
                        return DEFAULT_MEMORY_MODE == "cloud"
                """),
            },
            {
                "file": "config.py",
                "lines": "30-45",
                "category": "config",
                "name": "MEMORY_MODE",
                "content": textwrap.dedent("""\
                    MEMORY_MODE = os.getenv("MEMORY_MODE", "hybrid")
                    SYNTHESIZE_WITH_CLOUD = MEMORY_MODE in ("cloud", "hybrid")
                    DEFAULT_MEMORY_MODE = MEMORY_MODE
                    CLOUD_ESCALATION_WORD_COUNT = 40
                    CLOUD_ESCALATION_KEYWORDS = ["architecture", "explain", "how does", "overview"]
                """),
            },
            {
                "file": "main.py",
                "lines": "1495-1520",
                "category": "function",
                "name": "_query_deepseek",
                "content": textwrap.dedent("""\
                    async def _query_deepseek(user_query: str, context: str) -> str:
                        system_msg = _build_system_message()
                        response = await ds_client.chat.completions.create(
                            model=DEEPSEEK_MODEL,
                            messages=[
                                {"role": "system", "content": system_msg},
                                {"role": "user", "content": f"{context}\\n\\nQuestion: {user_query}"},
                            ],
                        )
                        return response.choices[0].message.content
                """),
            },
        ],
    },
    {
        "query": "Where is the .brain directory path defined and how is it resolved?",
        "entities": [
            {
                "file": "config.py",
                "lines": "1-15",
                "category": "config",
                "name": "DB_PATH",
                "content": textwrap.dedent("""\
                    DB_PATH = '.brain/'
                    CHROMA_COLLECTION_PREFIX = 'memory_'
                    BRAIN_DIR = Path(DB_PATH)
                    BRAIN_DIR.mkdir(parents=True, exist_ok=True)
                """),
            },
            {
                "file": "main.py",
                "lines": "120-145",
                "category": "function",
                "name": "init_workspace",
                "content": textwrap.dedent("""\
                    async def init_workspace(workspace_path: str) -> dict:
                        path = Path(workspace_path).resolve()
                        db_path = path / DB_PATH
                        db_path.mkdir(parents=True, exist_ok=True)
                        client = chromadb.PersistentClient(path=str(db_path))
                        collection_name = f"{CHROMA_COLLECTION_PREFIX}{workspace_id}"
                        collection = client.get_or_create_collection(collection_name)
                        return {"workspace_id": workspace_id, "db_path": str(db_path)}
                """),
            },
        ],
    },
    {
        "query": "How does the background brief synthesis avoid MCP timeouts?",
        "entities": [
            {
                "file": "main.py",
                "lines": "784-810",
                "category": "function",
                "name": "_background_brief_synthesis",
                "content": textwrap.dedent("""\
                    async def _background_brief_synthesis(workspace_id: str, workspace_path: str):
                        \"\"\"Fire-and-forget brief synthesis to avoid MCP timeout.\"\"\"
                        asyncio.create_task(
                            _synthesize_deep_brief(workspace_id, workspace_path)
                        )
                        return {"status": "brief_synthesis_started", "workspace_id": workspace_id}
                """),
            },
            {
                "file": "main.py",
                "lines": "538-570",
                "category": "function",
                "name": "_synthesize_deep_brief",
                "content": textwrap.dedent("""\
                    async def _synthesize_deep_brief(workspace_id: str, workspace_path: str):
                        sections = BRIEF_SECTIONS  # 9 locked section names
                        tasks = [_build_section(name, workspace_id, workspace_path) for name in sections]
                        results = await asyncio.gather(*tasks, return_exceptions=True)
                        brief_md = \"\\n\\n\".join(r for r in results if isinstance(r, str))
                        brief_path = BRAIN_DIR / "contexts" / f"{workspace_id}.md"
                        brief_path.write_text(brief_md, encoding="utf-8")
                """),
            },
        ],
    },
]

SYSTEM_PROMPT = textwrap.dedent("""\
    You are zerikai_memory, an intelligent code memory assistant.
    You answer developer questions about a codebase using retrieved context entities.
    Always cite sources inline using the format #file:line (e.g. #main.py:986).
    Be concise and precise. Do not hallucinate file names or line numbers.
    If the context does not contain enough information, say so explicitly.
""")

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    model: str
    query: str
    sample_idx: int
    latency_s: float
    answer: str
    error: Optional[str] = None

@dataclass
class ModelStats:
    model: str
    query: str
    latencies: list[float] = field(default_factory=list)
    answers: list[str] = field(default_factory=list)
    errors: int = 0

    @property
    def mean_latency(self) -> float:
        return statistics.mean(self.latencies) if self.latencies else 0.0

    @property
    def stdev_latency(self) -> float:
        return statistics.stdev(self.latencies) if len(self.latencies) > 1 else 0.0

    @property
    def min_latency(self) -> float:
        return min(self.latencies) if self.latencies else 0.0

    @property
    def max_latency(self) -> float:
        return max(self.latencies) if self.latencies else 0.0

# ---------------------------------------------------------------------------
# Ollama client — mirrors ol_client.generate used in _query_ollama
# ---------------------------------------------------------------------------

def build_user_prompt(entities: list[dict], query: str) -> str:
    """Assembles ChromaDB entity payloads into the user turn, same structure
    as zerikai_memory's context assembly before passing to the LLM."""
    ctx_parts = []
    for e in entities:
        ctx_parts.append(
            f"Entity: {e['name']}\n"
            f"File: {e['file']} (lines {e['lines']})\n"
            f"Category: {e['category']}\n"
            f"---\n{e['content']}"
        )
    context_block = "\n\n".join(ctx_parts)
    return f"Context from codebase memory:\n\n{context_block}\n\nQuestion: {query}"


def query_ollama(host: str, model: str, system: str, user: str) -> tuple[str, float]:
    """Single blocking call to Ollama /api/chat. Returns (answer, latency_s)."""
    url = f"{host}/api/chat"
    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    t0 = time.perf_counter()
    resp = requests.post(url, json=payload, timeout=120)
    latency = time.perf_counter() - t0
    resp.raise_for_status()
    data = resp.json()
    answer = data["message"]["content"]
    return answer, latency


def check_model_available(host: str, model: str) -> bool:
    """Check if a model is pulled in Ollama."""
    try:
        resp = requests.get(f"{host}/api/tags", timeout=10)
        resp.raise_for_status()
        names = [m["name"] for m in resp.json().get("models", [])]
        # Ollama may store as 'mistral:7b' or 'mistral:latest' etc.
        return any(model in n for n in names)
    except Exception:
        return False

# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------

def run_benchmark(
    models: list[str],
    n_samples: int,
    host: str,
) -> list[RunResult]:
    results: list[RunResult] = []

    for sample_data in CHROMADB_CONTEXT_SAMPLES:
        query = sample_data["query"]
        entities = sample_data["entities"]
        user_prompt = build_user_prompt(entities, query)

        print(f"\n{'='*70}")
        print(f"QUERY: {query}")
        print(f"{'='*70}")

        for model in models:
            print(f"\n  Model: {model}")
            for i in range(n_samples):
                print(f"    Sample {i+1}/{n_samples} ... ", end="", flush=True)
                try:
                    answer, latency = query_ollama(host, model, SYSTEM_PROMPT, user_prompt)
                    print(f"{latency:.2f}s")
                    results.append(RunResult(
                        model=model,
                        query=query,
                        sample_idx=i,
                        latency_s=latency,
                        answer=answer,
                    ))
                except Exception as e:
                    print(f"ERROR: {e}")
                    results.append(RunResult(
                        model=model,
                        query=query,
                        sample_idx=i,
                        latency_s=0.0,
                        answer="",
                        error=str(e),
                    ))

    return results

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def print_report(results: list[RunResult], models: list[str]) -> None:
    queries = list(dict.fromkeys(r.query for r in results))

    print(f"\n\n{'='*70}")
    print("BENCHMARK REPORT — zerikai_memory synthesis")
    print(f"{'='*70}")

    for query in queries:
        print(f"\nQuery: {query}")
        print("-" * 60)

        for model in models:
            runs = [r for r in results if r.model == model and r.query == query]
            good = [r for r in runs if not r.error]
            errors = [r for r in runs if r.error]

            if not good:
                print(f"  {model}: ALL FAILED — {errors[0].error if errors else 'unknown'}")
                continue

            latencies = [r.latency_s for r in good]
            mean_l = statistics.mean(latencies)
            stdev_l = statistics.stdev(latencies) if len(latencies) > 1 else 0.0

            print(f"\n  [{model}]")
            print(f"    Latency  mean={mean_l:.2f}s  stdev={stdev_l:.2f}s  "
                  f"min={min(latencies):.2f}s  max={max(latencies):.2f}s")
            if errors:
                print(f"    Errors: {len(errors)}/{len(runs)}")

            # Show last answer (most warmed-up cache, most representative)
            last_answer = good[-1].answer
            preview = last_answer[:400].replace("\n", " ")
            print(f"    Answer preview: {preview}{'...' if len(last_answer) > 400 else ''}")

            # Citation check — does the answer include #file:line refs?
            has_citations = "#" in last_answer and "." in last_answer
            print(f"    Citations detected: {'YES' if has_citations else 'NO — may need prompt tuning'}")

    # Summary table
    print(f"\n\n{'='*70}")
    print("LATENCY SUMMARY (all queries averaged)")
    print(f"{'='*70}")
    print(f"{'Model':<20} {'Mean (s)':<12} {'StdDev (s)':<12} {'Errors'}")
    print("-" * 55)
    for model in models:
        runs = [r for r in results if r.model == model and not r.error]
        errs = [r for r in results if r.model == model and r.error]
        if runs:
            all_lat = [r.latency_s for r in runs]
            print(f"{model:<20} {statistics.mean(all_lat):<12.2f} "
                  f"{(statistics.stdev(all_lat) if len(all_lat)>1 else 0.0):<12.2f} {len(errs)}")
        else:
            print(f"{model:<20} {'N/A':<12} {'N/A':<12} {len(errs)}")


def save_json(results: list[RunResult], path: str = "benchmark_results.json") -> None:
    data = [
        {
            "model": r.model,
            "query": r.query,
            "sample_idx": r.sample_idx,
            "latency_s": r.latency_s,
            "answer": r.answer,
            "error": r.error,
        }
        for r in results
    ]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"\nFull results saved to: {path}")

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="zerikai_memory local synthesis benchmark")
    parser.add_argument("--models", nargs="+", default=DEFAULT_MODELS,
                        help="Ollama model names to benchmark")
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES,
                        help="Number of runs per model per query")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help="Ollama server URL")
    parser.add_argument("--skip-check", action="store_true",
                        help="Skip model availability check")
    args = parser.parse_args()

    print(f"zerikai_memory synthesis benchmark")
    print(f"Host   : {args.host}")
    print(f"Models : {', '.join(args.models)}")
    print(f"Samples: {args.samples} per model per query")
    print(f"Queries: {len(CHROMADB_CONTEXT_SAMPLES)}")

    if not args.skip_check:
        print("\nChecking model availability...")
        for model in args.models:
            available = check_model_available(args.host, model)
            status = "OK" if available else "NOT FOUND — run: ollama pull " + model
            print(f"  {model}: {status}")
            if not available:
                print(f"\nAbort: pull missing models first or use --skip-check to bypass.")
                sys.exit(1)

    results = run_benchmark(args.models, args.samples, args.host)
    print_report(results, args.models)
    save_json(results)


if __name__ == "__main__":
    main()
