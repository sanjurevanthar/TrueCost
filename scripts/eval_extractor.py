"""Measure code-extraction accuracy on synthetic notes with answer keys (scripts/extraction_eval_notes.json).

  uv run python scripts/eval_extractor.py                  # keyword baseline only (no API key needed)
  uv run python scripts/eval_extractor.py --gemini         # needs GEMINI_API_KEY (read from .env)
  uv run python scripts/eval_extractor.py --claude         # needs ANTHROPIC_API_KEY
  ... --gemini --model gemini-3.8-flash                    # try another model

Reports per-note misses/extras plus overall precision, recall, F1 and exact-match rate.
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.repositories.sqlite_repo import SqlitePriceRepository  # noqa: E402
from app.services.claude_extractor import ClaudeCodeExtractor  # noqa: E402
from app.services.extraction import KeywordCodeExtractor  # noqa: E402
from app.services.gemini_extractor import GeminiCodeExtractor  # noqa: E402

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")


def score(name, extractor, notes):
    tp = fp = fn = exact = tin = tout = fb = 0
    t0 = time.time()
    print(f"\n=== {name} ===")
    for n in notes:
        res = extractor.extract_with_usage(n["note"])
        got = {c.code for c in res.codes}
        if res.usage:
            tin, tout = tin + res.usage.input_tokens, tout + res.usage.output_tokens
        fb += res.fallback_used
        exp = set(n["expected"])
        tp, fp, fn = tp + len(got & exp), fp + len(got - exp), fn + len(exp - got)
        ok = got == exp
        exact += ok
        print(f"{'OK  ' if ok else 'MISS'} {n['id']:4} missed={sorted(exp - got) or '-'} extra={sorted(got - exp) or '-'}")
    p = tp / (tp + fp) if tp + fp else 1.0
    r = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    print(f"precision {p:.0%} | recall {r:.0%} | F1 {f1:.0%} | exact {exact}/{len(notes)} | {time.time() - t0:.1f}s")
    if tin or tout:
        print(f"tokens: input {tin:,} | output {tout:,} | total {tin + tout:,} | per note ~{(tin + tout) // len(notes):,}"
              f"{f' | FALLBACKS: {fb}' if fb else ''}")
    return f1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--claude", action="store_true", help="also evaluate the Claude extractor (needs API credentials)")
    ap.add_argument("--gemini", action="store_true", help="also evaluate the Gemini extractor (needs GEMINI_API_KEY)")
    ap.add_argument("--model", help="model id (default: each extractor's default)")
    ap.add_argument("--effort", default="low", choices=["low", "medium", "high", "xhigh", "max"])
    a = ap.parse_args()
    db = ROOT / "truecost.db"
    if not db.exists():
        sys.exit("truecost.db not found - run scripts/build_db.py first.")
    notes = json.loads((Path(__file__).parent / "extraction_eval_notes.json").read_text())
    repo = SqlitePriceRepository(db)
    score("keyword baseline", KeywordCodeExtractor(repo), notes)
    # No fallback in the eval: a failure must show up as an error, not be hidden by the keyword matcher.
    class NoFallback(KeywordCodeExtractor):
        def extract(self, note):
            raise RuntimeError("LLM call failed")

    if a.claude:
        ex = ClaudeCodeExtractor(repo, model=a.model or "claude-opus-5-5", effort=a.effort, fallback=NoFallback(repo))
        score(f"{ex.name} (effort={a.effort})", ex, notes)
    if a.gemini:
        ex = GeminiCodeExtractor(repo, **({"model": a.model} if a.model else {}), fallback=NoFallback(repo))
        score(ex.name, ex, notes)


if __name__ == "__main__":
    main()
