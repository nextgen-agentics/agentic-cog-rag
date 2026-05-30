"""
index_markdown_docs.py
======================
Batch-index every .md file found under a given folder into the FAISS-backed
memory store, reusing the exact same chunking and embedding pipeline that the
MCP tool `index_document` uses.

Usage
-----
    python index_markdown_docs.py                          # uses default path
    python index_markdown_docs.py <path-to-markdown-dir>  # explicit path

Default markdown folder:
    <agent-dir>/sandbox/docs/markdown/

The script resolves each file to a sandbox-relative path and delegates to
`index_document()` from mcp_server.py so chunking, embedding, and FAISS
persistence are all handled by the existing production code.

Output
------
  - Per-file progress lines: indexed / skipped / error
  - Summary table at the end

Requirements
------------
  The virtual-env used to run the agent (.venv) must already have:
    faiss-cpu, sentence-transformers (or the gateway), pydantic, etc.
  Run from inside the agent directory or set PYTHONPATH accordingly:
      cd code/agent && python index_markdown_docs.py
"""

from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

# ── path bootstrap ────────────────────────────────────────────────────────────
# Make sure the agent package (memory, vector_index, gateway, …) is importable
# regardless of where the script is invoked from.
AGENT_DIR = Path(__file__).parent.resolve()
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

# ── import the production index_document function ─────────────────────────────
# We import the *function* directly (not going through MCP stdio) so we reuse
# the exact same chunking + embedding + FAISS-write path.
from mcp_server import index_document, SANDBOX  # noqa: E402

# ── default markdown root ─────────────────────────────────────────────────────
DEFAULT_MD_ROOT = SANDBOX / "docs" / "markdown"


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────

def _find_md_files(root: Path) -> list[Path]:
    """Recursively collect every .md file under *root*."""
    return sorted(root.rglob("*.md"))


def _to_sandbox_rel(abs_path: Path) -> str:
    """Convert an absolute path to a SANDBOX-relative POSIX string.

    index_document() receives paths relative to SANDBOX, e.g.
      'docs/markdown/100/Kuruppu et al. - 2025 - EEG ...md'
    """
    return abs_path.relative_to(SANDBOX).as_posix()


def _hr(seconds: float) -> str:
    """Human-readable duration."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s}s"


# ─────────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────────

def main(md_root: Path) -> None:
    if not md_root.exists():
        print(f"[ERROR] Directory not found: {md_root}", file=sys.stderr)
        sys.exit(1)
    if not md_root.is_dir():
        print(f"[ERROR] Not a directory: {md_root}", file=sys.stderr)
        sys.exit(1)

    md_files = _find_md_files(md_root)
    total = len(md_files)

    if total == 0:
        print(f"No .md files found under: {md_root}")
        return

    print(f"\n{'='*70}")
    print(f"  Markdown Indexer — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Root  : {md_root}")
    print(f"  Files : {total}")
    print(f"{'='*70}\n")

    results: list[dict] = []
    t_start = time.perf_counter()

    for i, abs_path in enumerate(md_files, start=1):
        rel_path = _to_sandbox_rel(abs_path)
        size_kb = abs_path.stat().st_size / 1024
        label = f"[{i}/{total}]"

        print(f"{label} {rel_path}  ({size_kb:.1f} KB)", end=" ... ", flush=True)

        t0 = time.perf_counter()
        try:
            result = index_document(rel_path)
            elapsed = time.perf_counter() - t0

            chunks = result.get("chunks_indexed", 0)
            warning = result.get("warning", "")

            if warning:
                status = f"SKIPPED ({warning})"
            else:
                status = f"OK  {chunks} chunks"

            print(f"{status}  [{_hr(elapsed)}]")
            results.append({
                "path": rel_path,
                "status": "skipped" if warning else "ok",
                "chunks": chunks,
                "elapsed": elapsed,
                "warning": warning,
            })

        except Exception as exc:
            elapsed = time.perf_counter() - t0
            print(f"ERROR: {exc}  [{_hr(elapsed)}]")
            results.append({
                "path": rel_path,
                "status": "error",
                "chunks": 0,
                "elapsed": elapsed,
                "error": str(exc),
            })

    # ── summary ──────────────────────────────────────────────────────────────
    total_elapsed = time.perf_counter() - t_start
    ok      = [r for r in results if r["status"] == "ok"]
    skipped = [r for r in results if r["status"] == "skipped"]
    errors  = [r for r in results if r["status"] == "error"]
    total_chunks = sum(r["chunks"] for r in ok)

    print(f"\n{'='*70}")
    print(f"  Summary")
    print(f"  -------")
    print(f"  Files processed : {total}")
    print(f"  Indexed OK      : {len(ok)}  ({total_chunks} total chunks)")
    print(f"  Skipped (empty) : {len(skipped)}")
    print(f"  Errors          : {len(errors)}")
    print(f"  Total time      : {_hr(total_elapsed)}")
    print(f"{'='*70}\n")

    if errors:
        print("Files with errors:")
        for r in errors:
            print(f"  ✗  {r['path']}")
            print(f"     {r.get('error', '')}")
        print()

    if skipped:
        print("Skipped files:")
        for r in skipped:
            print(f"  –  {r['path']}  ({r.get('warning','')})")
        print()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        md_root_arg = Path(sys.argv[1]).expanduser().resolve()
    else:
        md_root_arg = DEFAULT_MD_ROOT

    main(md_root_arg)
