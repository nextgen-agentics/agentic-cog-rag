#!/usr/bin/env python3
"""
convert_pdfs_to_markdown.py

Traverses a folder (default: docs/Exported Items/files/), finds all PDF
documents recursively, and converts each one to Markdown using Microsoft's
markitdown library.

Converted .md files are saved alongside the source PDFs (same base name,
.md extension) unless --output-dir is supplied.

Usage:
    python convert_pdfs_to_markdown.py
    python convert_pdfs_to_markdown.py --input-dir "sandbox/docs/Exported Items/files/79"
    python convert_pdfs_to_markdown.py --output-dir "sandbox/docs/markdown"
    python convert_pdfs_to_markdown.py --overwrite
    python convert_pdfs_to_markdown.py --verbose

Requirements:
    pip install markitdown[pdf]
    # or for full format support:
    pip install "markitdown[all]"
"""

import argparse
import logging
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Convert PDF files to Markdown using markitdown. "
            "Paths with spaces must be quoted: --input-dir \"my folder/files\""
        )
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path(__file__).parent / "docs" / "Exported Items" / "files",
        help=(
            "Root folder to scan for PDFs (default: docs/Exported Items/files "
            "relative to this script). Quote paths that contain spaces."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help=(
            "Directory to write .md files into. "
            "If omitted, each .md is saved next to its source PDF. "
            "Subfolder structure relative to --input-dir is preserved."
        ),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-convert and overwrite existing .md files (default: skip).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging.",
    )
    return parser.parse_args()


def convert_pdf(pdf_path: Path, md_path: Path, converter) -> bool:
    """
    Convert a single PDF to Markdown using markitdown.

    Returns True on success, False on failure.
    """
    try:
        result = converter.convert(str(pdf_path))
        markdown_text = result.text_content

        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(markdown_text, encoding="utf-8")
        return True

    except Exception as exc:  # noqa: BLE001
        log.error("  ✗ Failed to convert '%s': %s", pdf_path.name, exc)
        return False


def main() -> None:
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Import here so a missing install gives a clean error message
    try:
        from markitdown import MarkItDown  # type: ignore
    except ImportError:
        log.error(
            "markitdown is not installed.\n"
            "Install it with:  pip install 'markitdown[pdf]'\n"
            "For all formats: pip install 'markitdown[all]'"
        )
        sys.exit(1)

    input_dir: Path = args.input_dir.resolve()

    if not input_dir.exists():
        log.error("Input directory does not exist: %s", input_dir)
        sys.exit(1)

    log.info("Scanning for PDFs in: %s", input_dir)

    pdf_files = sorted(input_dir.rglob("*.pdf"))

    if not pdf_files:
        log.warning("No PDF files found under %s", input_dir)
        return

    log.info("Found %d PDF file(s).", len(pdf_files))

    # Create a single converter instance (reused across all files)
    converter = MarkItDown()

    success_count = 0
    skip_count = 0
    fail_count = 0

    for pdf_path in pdf_files:
        # Determine output path
        if args.output_dir is not None:
            relative = pdf_path.relative_to(input_dir)
            md_path = args.output_dir.resolve() / relative.with_suffix(".md")
        else:
            md_path = pdf_path.with_suffix(".md")

        if md_path.exists() and not args.overwrite:
            log.info("SKIP  (already exists) → %s", md_path.name)
            skip_count += 1
            continue

        log.info("Converting: %s", pdf_path.name)
        ok = convert_pdf(pdf_path, md_path, converter)

        if ok:
            log.info("  ✓ Saved  → %s", md_path)
            success_count += 1
        else:
            fail_count += 1

    # Summary
    log.info(
        "Done. Converted: %d | Skipped: %d | Failed: %d",
        success_count,
        skip_count,
        fail_count,
    )

    if fail_count:
        sys.exit(1)


if __name__ == "__main__":
    main()
