#!/usr/bin/env python3
"""
Kindle Scribe Library Reader
A local server to browse handwritten notebooks AND books from your Kindle.

Major sections of this file (in order):
  1. Imports & app setup
  2. Library configuration (paths, migration)
  3. Discovery + caching of notebooks and books
  4. Conversion pipelines (notebook → PDF, book → PDF or raw)
  5. Flask routes (UI + API)
  6. HTML/CSS/JS bundle
  7. CLI

Usage:
    python server.py                    # auto-detect, sync nothing, just serve
    python server.py --sync             # sync notebooks AND books before serving
    python server.py --sync-notebooks   # only notebooks
    python server.py --sync-books       # only books
"""

import argparse
import hashlib
import json
import logging
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from flask import (Flask, abort, jsonify, render_template_string, request,
                   send_file)

import nbk_to_pdf
import kfx_to_pdf
import library

app = Flask(__name__)
log = logging.getLogger(__name__)

# Cap upload size at the same limit as MTP push (100MB)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024


# ── Library configuration ────────────────────────────────────────────────────

# Library lives next to the app so it's easy to find, back up, or remove.
# Users who prefer the old hidden-folder layout can pass --library to override.
APP_DIR = Path(__file__).resolve().parent
LIBRARY_ROOT = APP_DIR / "library"

# Legacy locations we'll auto-migrate from on first run, in order of priority.
# We move content from these into the new library/ folder if found.
LEGACY_PATHS = [
    Path.home() / ".scribe_library",      # previous version's location
    Path.home() / ".scribe_notebooks",    # the version before that
]

# Cache for converted files (PDFs, page JPEGs, extracted covers) — separate
# from the library so users can blow it away without losing original content.
CACHE_DIR = Path(tempfile.gettempdir()) / "scribe_reader_cache"
CACHE_DIR.mkdir(exist_ok=True)

# Populated at startup
PATHS: library.LibraryPaths | None = None


def init_library(root: Path = LIBRARY_ROOT) -> library.LibraryPaths:
    """Initialize on-disk layout. Migrates from older locations on first run."""
    paths = library.LibraryPaths(root)
    paths.ensure_exists()

    for legacy in LEGACY_PATHS:
        if legacy == paths.root:
            continue  # don't migrate from ourselves

        # If legacy is a *.scribe_library, it has the same internal layout —
        # migrate the whole tree (notebooks/ and books/ subdirs).
        if legacy.name == ".scribe_library" and legacy.is_dir():
            moved = _migrate_full_library(legacy, paths)
            if moved:
                log.info("Migrated %d file(s) from %s to %s",
                         moved, legacy, paths.root)
        # If it's the old flat .scribe_notebooks, just move the .nbk + .png files
        elif legacy.is_dir():
            moved = paths.migrate_legacy(legacy)
            if moved:
                log.info("Migrated %d notebook(s) from %s to %s",
                         moved, legacy, paths.notebooks_dir)
    return paths


def _migrate_full_library(legacy_root: Path, new_paths: library.LibraryPaths) -> int:
    """
    Move a complete legacy library (notebooks/ + books/ subdirs) into the new
    location. Idempotent — files already present at the destination are
    skipped, not overwritten.
    """
    moved = 0
    legacy_layout = library.LibraryPaths(legacy_root)
    for legacy_dir, new_dir in [
        (legacy_layout.notebooks_dir, new_paths.notebooks_dir),
        (legacy_layout.purchased_dir, new_paths.purchased_dir),
        (legacy_layout.sideloaded_dir, new_paths.sideloaded_dir),
    ]:
        if not legacy_dir.exists():
            continue
        new_dir.mkdir(parents=True, exist_ok=True)
        for src in legacy_dir.iterdir():
            if not src.is_file():
                continue
            dst = new_dir / src.name
            if dst.exists():
                continue
            try:
                src.rename(dst)
                moved += 1
            except OSError as e:
                log.warning("Could not migrate %s: %s", src, e)

    # Move meta.json too if it exists
    if legacy_layout.meta_path.exists() and not new_paths.meta_path.exists():
        try:
            legacy_layout.meta_path.rename(new_paths.meta_path)
        except OSError:
            pass

    return moved


# ── Discovery ────────────────────────────────────────────────────────────────

def all_items() -> list[dict]:
    """Return everything in the library — notebooks, books, AND screenshots."""
    if not PATHS:
        return []
    cache = library.load_metadata_cache(PATHS)
    return (
        library.discover_notebooks(PATHS)
        + library.discover_books(PATHS, cache, CACHE_DIR)
        + library.discover_screenshots(PATHS)
    )


def find_item(item_id: str) -> dict | None:
    """Look up one item by its stable hash ID."""
    for item in all_items():
        if item["id"] == item_id:
            return item
    return None


# ── Conversion pipelines ─────────────────────────────────────────────────────

def _ensure_notebook_pdf(item: dict) -> Path:
    """Convert (or reuse cached) PDF for one notebook. Raises on failure."""
    nb_cache = CACHE_DIR / item["id"]
    nb_cache.mkdir(parents=True, exist_ok=True)
    pdf_path = nb_cache / "out.pdf"
    meta_path = nb_cache / "meta.json"

    src = Path(item["path"])
    try:
        mtime = src.stat().st_mtime
    except OSError as e:
        raise RuntimeError(f"Cannot read notebook file: {e}")

    # Cache hit?
    if pdf_path.exists() and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            if meta.get("mtime") == mtime and meta.get("ok"):
                return pdf_path
        except Exception:
            pass

    log.info("Converting notebook %s → %s", src, pdf_path)
    try:
        result = nbk_to_pdf.convert_or_raise(src, pdf_path)
    except nbk_to_pdf.ConversionError as e:
        lines = [f"[step: {e.step or 'unknown'}] {e}"]
        if e.kfx_errors:
            lines.append("\nkfxlib errors:")
            lines.extend(f"  • {m}" for m in e.kfx_errors[:10])
        if e.kfx_warnings:
            lines.append("\nkfxlib warnings (first 5):")
            lines.extend(f"  • {m}" for m in e.kfx_warnings[:5])
        raise RuntimeError("\n".join(lines))

    meta_path.write_text(json.dumps({"mtime": mtime, "ok": True,
                                     "pages": result.get("pages", 0)}))
    return pdf_path


def _ensure_book_pdf(item: dict) -> tuple[Path, str]:
    """
    Convert a book to a PDF the in-browser reader can serve.

    Returns (pdf_path, kind) where kind is:
       'pdf'      → a real PDF, in-browser viewing works
       'raw'      → not convertable; serve the raw file as a download
    Raises RuntimeError with a user-readable message if conversion fails.
    """
    nb_cache = CACHE_DIR / item["id"]
    nb_cache.mkdir(parents=True, exist_ok=True)
    pdf_path = nb_cache / "out.pdf"
    meta_path = nb_cache / "meta.json"
    src = Path(item["path"])

    try:
        mtime = src.stat().st_mtime
    except OSError as e:
        raise RuntimeError(f"Cannot read book file: {e}")

    if pdf_path.exists() and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            if meta.get("mtime") == mtime and meta.get("ok"):
                return pdf_path, meta.get("kind", "pdf")
        except Exception:
            pass

    ext = item.get("extension", "").lower()

    log.info("Converting book %s (%s) → %s", src, ext, pdf_path)
    try:
        kfx_to_pdf.convert_to_viewable(src, pdf_path)
        kind = "pdf"
    except kfx_to_pdf.BookConvertError as e:
        # Don't write a bad PDF to cache — but DO record that this file isn't
        # convertable so we don't keep retrying. Mark it as 'raw' so the API
        # returns a download URL instead of pages.
        meta_path.write_text(json.dumps({
            "mtime": mtime, "ok": True, "kind": "raw",
            "reason": str(e), "step": e.step,
        }))
        return src, "raw"

    meta_path.write_text(json.dumps({"mtime": mtime, "ok": True, "kind": kind}))
    return pdf_path, kind


def _pdf_to_page_images(pdf_path: Path, out_dir: Path) -> list[Path]:
    """
    Extract per-page JPEGs from a PDF for inline viewing in the reader.

    For PDFs we built ourselves (via nbk_to_pdf or kfx_to_pdf), every page is
    a single embedded image, so we extract those images directly. For
    arbitrary PDFs (like sideloaded books) we fall back to PyMuPDF/pdf2image
    if available, otherwise we skip image extraction and let the browser's
    native PDF viewer handle display via the /pdf/ endpoint.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    cached = sorted(out_dir.glob("page_*.jpg"))
    if cached:
        return cached

    # Strategy 1: pull embedded images out of the PDF (fast for our PDFs)
    page_files = _extract_embedded_page_images(pdf_path, out_dir)
    if page_files:
        return page_files

    # Strategy 2: rasterize the PDF page-by-page if a renderer is available
    page_files = _rasterize_pdf_pages(pdf_path, out_dir)
    return page_files


def _extract_embedded_page_images(pdf_path: Path, out_dir: Path) -> list[Path]:
    """For PDFs with one image per page, pull those out directly."""
    try:
        import pypdf
        from io import BytesIO
        from PIL import Image

        reader = pypdf.PdfReader(str(pdf_path))
        page_files: list[Path] = []
        for i, page in enumerate(reader.pages):
            imgs = list(page.images)
            if len(imgs) != 1:
                # Page has zero or multiple images — not our format, bail
                # to let the rasterizer take over.
                return []
            try:
                img = Image.open(BytesIO(imgs[0].data)).convert("RGB")
            except Exception as e:
                log.warning("Page %d image decode failed: %s", i, e)
                continue
            page_file = out_dir / f"page_{i:04d}.jpg"
            img.save(page_file, "JPEG", quality=88, optimize=True)
            img.close()
            page_files.append(page_file)
        return page_files
    except Exception as e:
        log.debug("Embedded-image extraction failed: %s", e)
        return []


def _rasterize_pdf_pages(pdf_path: Path, out_dir: Path) -> list[Path]:
    """
    Rasterize every page of a PDF using PyMuPDF (fitz) if available.
    PyMuPDF is the only renderer we attempt — it's a single pip install,
    bundles MuPDF, and works without external system dependencies.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        log.info("PyMuPDF not installed — skipping per-page rasterization. "
                 "The browser's built-in PDF viewer will be used instead.")
        return []

    page_files: list[Path] = []
    try:
        doc = fitz.open(str(pdf_path))
        # Render at ~150 DPI, which is enough for screen reading.
        zoom = 150 / 72
        matrix = fitz.Matrix(zoom, zoom)
        for i, page in enumerate(doc):
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            page_file = out_dir / f"page_{i:04d}.jpg"
            pix.pil_save(str(page_file), format="JPEG", quality=85, optimize=True)
            page_files.append(page_file)
        doc.close()
    except Exception as e:
        log.error("PDF rasterization failed: %s", e)
        return []

    return page_files


# ── Routes: UI ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML_UI)


# ── Routes: discovery ────────────────────────────────────────────────────────

@app.route("/api/library")
def api_library():
    """Return all items, split by kind."""
    cache = library.load_metadata_cache(PATHS)
    return jsonify({
        "notebooks": library.discover_notebooks(PATHS),
        "books": library.discover_books(PATHS, cache, CACHE_DIR),
        "screenshots": library.discover_screenshots(PATHS),
    })


@app.route("/api/status")
def api_status():
    try:
        from mtp_sync import detect_device
        device = detect_device()
    except Exception:
        device = ""

    nb_count = len(library.discover_notebooks(PATHS))
    book_count = len(library.discover_books(PATHS, library.load_metadata_cache(PATHS), CACHE_DIR))
    screenshot_count = len(library.discover_screenshots(PATHS))
    converter_msg = nbk_to_pdf._check_deps() or ""

    # Book reader status — distinct from the notebook-decoder check
    book_reader_ready = kfx_to_pdf._check_weasyprint_available()
    book_reader_msg = "" if book_reader_ready else (
        "WeasyPrint not installed — text-format books (KFX/AZW3/EPUB) will "
        "show as download-only. Install with: pip3 install weasyprint"
    )

    cache_size = 0
    if CACHE_DIR.exists():
        cache_size = sum(
            f.stat().st_size for f in CACHE_DIR.rglob("*") if f.is_file()
        )

    return jsonify({
        "device_connected": bool(device),
        "device_name": device or None,
        "library_root": str(LIBRARY_ROOT),
        "notebook_count": nb_count,
        "book_count": book_count,
        "screenshot_count": screenshot_count,
        "converter_ready": not converter_msg,
        "converter_message": converter_msg,
        "book_reader_ready": book_reader_ready,
        "book_reader_message": book_reader_msg,
        "cache_size_mb": round(cache_size / 1024 / 1024, 2),
    })


# ── Routes: notebook reader ──────────────────────────────────────────────────

@app.route("/api/notebook/<item_id>")
def api_notebook(item_id):
    item = find_item(item_id)
    if not item or item["kind"] != "notebook":
        abort(404)
    try:
        pdf_path = _ensure_notebook_pdf(item)
        nb_cache = CACHE_DIR / item_id
        page_files = _pdf_to_page_images(pdf_path, nb_cache)
    except Exception as e:
        return jsonify({"error": str(e), **item}), 500

    return jsonify({
        **item,
        "pages": [f"/page/{item_id}/{i:04d}" for i in range(len(page_files))],
        "pdf_url": f"/pdf/{item_id}",
    })


@app.route("/thumbnail/<item_id>")
def serve_thumbnail(item_id):
    """Serve the first-page thumbnail PNG for a notebook (sidebar previews)."""
    item = find_item(item_id)
    if not item or item["kind"] != "notebook":
        abort(404)
    nb_path = Path(item["path"])
    thumb = nb_path.with_suffix(".png")
    if not thumb.exists():
        abort(404)
    return send_file(str(thumb), mimetype="image/png")


# ── Routes: book reader ──────────────────────────────────────────────────────

@app.route("/api/book/<item_id>")
def api_book(item_id):
    item = find_item(item_id)
    if not item or item["kind"] != "book":
        abort(404)

    # Lazy-extract metadata on first open if we haven't already
    cache = library.load_metadata_cache(PATHS)
    if item_id not in cache:
        _try_extract_metadata(item)

    try:
        pdf_path, kind = _ensure_book_pdf(item)
    except Exception as e:
        return jsonify({"error": str(e), **item}), 500

    if kind == "raw":
        # Not viewable in-browser — only a download is offered
        return jsonify({
            **item,
            "pages": [],
            "viewable": False,
            "download_url": f"/raw/{item_id}",
            "message": (
                "This format isn't supported for in-browser reading yet. "
                "Use the Download button to save the file and open it in "
                "another reader."
            ),
        })

    nb_cache = CACHE_DIR / item_id
    page_files = _pdf_to_page_images(pdf_path, nb_cache)

    return jsonify({
        **item,
        "pages": [f"/page/{item_id}/{i:04d}" for i in range(len(page_files))],
        "pdf_url": f"/pdf/{item_id}",
        "viewable": True,
        # If page extraction didn't work, the UI will fall back to the
        # browser's native PDF viewer via the pdf_url.
        "fallback_pdf": len(page_files) == 0,
    })


@app.route("/cover/<item_id>")
def serve_cover(item_id):
    """
    Serve a cover image for a book or notebook. Resolution order:
      1. Cached extracted cover (cover.jpg/png from book metadata)
      2. Notebook thumbnail PNG (sibling .png file)
      3. First page of the converted PDF (page_0000.jpg) — the fallback
         that makes KFX/EPUB books with no metadata cover still get a tile
    """
    item_dir = CACHE_DIR / item_id

    # 1. Real cover from metadata
    cover_files = sorted(item_dir.glob("cover.*"))
    if cover_files:
        return send_file(str(cover_files[0]))

    item = find_item(item_id)
    if not item:
        abort(404)

    # 2. Notebook thumbnail — for notebooks this is the device-generated PNG
    if item["kind"] == "notebook":
        thumb = Path(item["path"]).with_suffix(".png")
        if thumb.exists():
            return send_file(str(thumb), mimetype="image/png")

    # 3. For books, try to extract real cover from metadata once
    if item["kind"] == "book":
        if _try_extract_metadata(item):
            cover_files = sorted(item_dir.glob("cover.*"))
            if cover_files:
                return send_file(str(cover_files[0]))

    # 4. Fallback: first page of the converted PDF
    first_page = item_dir / "page_0000.jpg"
    if first_page.exists():
        return send_file(str(first_page), mimetype="image/jpeg")

    abort(404)


def _try_extract_metadata(item: dict) -> bool:
    """Extract metadata for one book and persist title/author/cover."""
    src = Path(item["path"])
    try:
        md = kfx_to_pdf.extract_metadata(src)
    except Exception as e:
        log.debug("Metadata extraction failed for %s: %s", src, e)
        return False

    nb_cache = CACHE_DIR / item["id"]
    nb_cache.mkdir(parents=True, exist_ok=True)

    has_cover = False
    if md.get("cover_bytes"):
        ext = (md.get("cover_ext") or "jpg").lstrip(".").lower()
        if ext not in ("jpg", "jpeg", "png", "gif", "webp"):
            ext = "jpg"
        cover_path = nb_cache / f"cover.{ext}"
        try:
            cover_path.write_bytes(md["cover_bytes"])
            has_cover = True
        except OSError:
            pass

    library.update_book_metadata(
        PATHS, item["id"],
        title=md.get("title"),
        authors=md.get("authors") or [],
        has_cover=has_cover,
    )
    return True


# ── Routes: shared (page images, raw downloads, PDFs) ────────────────────────

@app.route("/page/<item_id>/<page_num>")
def serve_page(item_id, page_num):
    page_file = CACHE_DIR / item_id / f"page_{page_num}.jpg"
    if not page_file.exists():
        abort(404)
    return send_file(str(page_file), mimetype="image/jpeg")


@app.route("/pdf/<item_id>")
def serve_pdf(item_id):
    """Serve the converted PDF (or original PDF for sideloaded books)."""
    pdf_file = CACHE_DIR / item_id / "out.pdf"
    if pdf_file.exists():
        return send_file(str(pdf_file), mimetype="application/pdf")
    # Fall back to the original file if it's already a PDF
    item = find_item(item_id)
    if item and Path(item["path"]).suffix.lower() == ".pdf":
        return send_file(item["path"], mimetype="application/pdf")
    abort(404)


@app.route("/raw/<item_id>")
def serve_raw(item_id):
    """Serve the original book file as a download (for non-PDF formats)."""
    item = find_item(item_id)
    if not item:
        abort(404)
    src = Path(item["path"])
    if not src.exists():
        abort(404)
    return send_file(
        str(src),
        as_attachment=True,
        download_name=src.name,
    )


# ── Routes: sync ─────────────────────────────────────────────────────────────

@app.route("/api/sync_notebooks", methods=["POST"])
def api_sync_notebooks():
    try:
        from mtp_sync import sync_notebooks
    except ImportError:
        return jsonify({"error": "mtp_sync.py not found"}), 500

    log_lines: list[str] = []
    report = sync_notebooks(dest=PATHS.notebooks_dir, log=log_lines.append)
    return jsonify({
        "ok": report.notebooks_found > 0,
        "kind": "notebooks",
        "device": report.device_name,
        "found": report.notebooks_found,
        "copied": report.notebooks_copied,
        "skipped": report.notebooks_skipped,
        "thumbnails": report.thumbnails_copied,
        "errors": report.errors,
        "log": log_lines,
    })


@app.route("/api/sync_books", methods=["POST"])
def api_sync_books():
    try:
        from mtp_sync import sync_books
    except ImportError:
        return jsonify({"error": "mtp_sync.py not found"}), 500

    log_lines: list[str] = []
    report = sync_books(
        dest=PATHS.books_dir,
        purchased_subdir=PATHS.purchased_dir.name,
        sideloaded_subdir=PATHS.sideloaded_dir.name,
        log=log_lines.append,
    )
    return jsonify({
        "ok": report.books_found > 0,
        "kind": "books",
        "device": report.device_name,
        "found": report.books_found,
        "copied": report.books_copied,
        "skipped": report.books_skipped,
        "errors": report.errors,
        "log": log_lines,
    })


@app.route("/api/sync_screenshots", methods=["POST"])
def api_sync_screenshots():
    try:
        from mtp_sync import sync_screenshots
    except ImportError:
        return jsonify({"error": "mtp_sync.py not found"}), 500

    log_lines: list[str] = []
    report = sync_screenshots(dest=PATHS.screenshots_dir, log=log_lines.append)
    return jsonify({
        "ok": report.screenshots_found > 0,
        "kind": "screenshots",
        "device": report.device_name,
        "found": report.screenshots_found,
        "copied": report.screenshots_copied,
        "skipped": report.screenshots_skipped,
        "errors": report.errors,
        "log": log_lines,
    })


# ── Routes: screenshots (view + delete) ──────────────────────────────────────

@app.route("/screenshot/<item_id>")
def serve_screenshot(item_id):
    """Serve a screenshot PNG/JPG full-size."""
    item = find_item(item_id)
    if not item or item.get("kind") != "screenshot":
        abort(404)
    src = Path(item["path"])
    if not src.exists():
        abort(404)
    mime = "image/png" if src.suffix.lower() == ".png" else "image/jpeg"
    return send_file(str(src), mimetype=mime)


@app.route("/api/screenshot/<item_id>/delete", methods=["POST"])
def api_delete_screenshot(item_id):
    """
    Delete a screenshot from the LOCAL library AND from the Kindle.

    Two-phase delete:
      1. Connect to the device (if attached) and DELETE_OBJECT it via MTP
      2. Remove the local file
    If step 1 fails (no device, file already gone, etc.) we still proceed
    with step 2 unless the request explicitly asks for atomic delete.
    """
    item = find_item(item_id)
    if not item or item.get("kind") != "screenshot":
        abort(404)

    src = Path(item["path"])
    filename = item["filename"]
    log_lines: list[str] = []
    device_errors: list[str] = []
    device_deleted = False
    device_name = ""

    try:
        from mtp_sync import delete_from_kindle
        report = delete_from_kindle(filename, log=log_lines.append)
        device_deleted = report.deleted
        device_errors = report.errors
        device_name = report.device_name
    except ImportError:
        device_errors = ["mtp_sync not available"]

    # Always remove the local file even if device delete failed.
    # If the user wanted to retry the device delete later, they can re-sync
    # to pull the file back, then delete again.
    local_deleted = False
    try:
        if src.exists():
            src.unlink()
            local_deleted = True
            log_lines.append(f"✓ Removed local copy: {src.name}")
    except OSError as e:
        log_lines.append(f"✗ Local delete failed: {e}")

    return jsonify({
        "ok": device_deleted and local_deleted,
        "local_deleted": local_deleted,
        "device_deleted": device_deleted,
        "device_name": device_name,
        "filename": filename,
        "errors": device_errors,
        "log": log_lines,
    })


# ── Routes: upload (push to Kindle) ──────────────────────────────────────────

@app.route("/api/upload", methods=["POST"])
def api_upload():
    """
    Receive a PDF (or other small file) from the browser and push it onto
    the Kindle's documents/ folder via MTP.
    """
    if "file" not in request.files:
        return jsonify({"error": "No file in request"}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400

    # Only allow safe extensions on push to keep the surface small.
    safe_extensions = {".pdf", ".epub", ".mobi", ".txt", ".docx", ".rtf"}
    ext = Path(f.filename).suffix.lower()
    if ext not in safe_extensions:
        return jsonify({
            "error": f"Refusing to push '{ext}' — only "
                     f"{', '.join(sorted(safe_extensions))} are allowed.",
        }), 400

    # Stage the upload to a temp file on disk; MTP send streams from a path
    tmp_dir = Path(tempfile.mkdtemp(prefix="scribe_upload_"))
    safe_name = library.safe_local_filename(f.filename)
    tmp_path = tmp_dir / safe_name
    try:
        f.save(str(tmp_path))
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify({"error": f"Could not stage upload: {e}"}), 500

    try:
        from mtp_sync import push_pdf_to_kindle
    except ImportError:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify({"error": "mtp_sync.py not found"}), 500

    log_lines: list[str] = []
    try:
        report = push_pdf_to_kindle(
            tmp_path, target_filename=safe_name, log=log_lines.append)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return jsonify({
        "ok": report.file_pushed,
        "device": report.device_name,
        "target": report.target_path,
        "bytes": report.bytes_sent,
        "errors": report.errors,
        "log": log_lines,
    })


# ── Routes: cache & metadata management ──────────────────────────────────────

@app.route("/api/clear_cache", methods=["POST", "GET"])
def clear_cache():
    shutil.rmtree(CACHE_DIR, ignore_errors=True)
    CACHE_DIR.mkdir(exist_ok=True)
    return jsonify({"ok": True})


@app.route("/api/extract_metadata/<item_id>", methods=["POST"])
def api_extract_metadata(item_id):
    """Extract title/author/cover for one book on demand."""
    item = find_item(item_id)
    if not item or item["kind"] != "book":
        abort(404)
    ok = _try_extract_metadata(item)
    return jsonify({"ok": ok, "item_id": item_id})


@app.route("/api/annotations/<item_id>")
def api_annotations(item_id):
    """
    Diagnostic endpoint: report whether a sideloaded PDF has an annotation
    sidecar and what we were able to decode from it. Returns:
        {
          has_sidecar: bool,
          sidecar_dir: str|null,
          sidecar_files: [(name, size), ...],
          decoded_pages: int,
          pages_with_strokes: [page_indices...],
          overlay_pages: int,
          error: str|null,
          log: [str, ...],
        }
    """
    import pdf_annotations
    item = find_item(item_id)
    if not item or item["kind"] != "book":
        abort(404)

    src = Path(item["path"])
    if src.suffix.lower() != ".pdf":
        return jsonify({
            "has_sidecar": False,
            "reason": "Annotations are only supported on sideloaded PDFs.",
        })

    sidecar = pdf_annotations.find_sidecar(src)
    if not sidecar:
        return jsonify({
            "has_sidecar": False,
            "pdf_path": str(src),
        })

    # Try to decode without writing the overlay (just diagnostics).
    try:
        data = pdf_annotations.decode_strokes(sidecar)
        debug = data.debug
        return jsonify({
            "has_sidecar": True,
            "sidecar_dir": str(sidecar),
            "sidecar_files": [
                {"name": n, "size": s} for n, s in debug.sidecar_files],
            "decoded_pages": debug.decoded_pages,
            "pages_with_strokes": debug.pages_with_strokes,
            "log": debug.log_lines,
        })
    except pdf_annotations.AnnotationError as e:
        return jsonify({
            "has_sidecar": True,
            "sidecar_dir": str(sidecar),
            "error": str(e),
            "step": e.step,
            "details": e.details,
        }), 500


# ── HTML UI ──────────────────────────────────────────────────────────────────

HTML_UI = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Scribe Library</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,400;0,600;1,400&family=DM+Mono:wght@300;400&family=DM+Sans:wght@300;400;500&display=swap');

  :root {
    --ink: #1a1410;
    --paper: #f5f0e8;
    --paper-dark: #ede7d7;
    --paper-border: #d4c9b0;
    --accent: #8b5e3c;
    --accent-light: #c4956a;
    --accent-pale: #f0e4d4;
    --text: #2d2318;
    --text-muted: #7a6a5a;
    --shadow: rgba(26,20,16,0.15);
    --page-bg: #fdfaf5;
    --book-accent: #4a6fa5;
    --book-accent-pale: #e0e8f5;
    --notebook-accent: #8b5e3c;
    --notebook-accent-pale: #f0e4d4;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: var(--paper);
    color: var(--text);
    font-family: 'DM Sans', sans-serif;
    font-weight: 300;
    min-height: 100vh;
    background-image: repeating-linear-gradient(
      0deg, transparent, transparent 27px, rgba(180,160,130,0.12) 28px);
  }

  .shell {
    display: grid;
    grid-template-columns: 320px 1fr;
    grid-template-rows: 60px 1fr;
    height: 100vh;
    overflow: hidden;
  }

  .topbar {
    grid-column: 1 / -1;
    background: var(--ink);
    display: flex;
    align-items: center;
    padding: 0 24px;
    gap: 12px;
    box-shadow: 0 2px 12px rgba(0,0,0,0.3);
    z-index: 100;
  }

  .logo {
    font-family: 'Playfair Display', serif;
    font-size: 1.35rem;
    color: var(--paper);
    letter-spacing: 0.02em;
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .logo-icon { width: 22px; height: 22px; opacity: 0.85; }

  .topbar-status {
    margin-left: auto;
    font-family: 'DM Mono', monospace;
    font-size: 0.7rem;
    color: rgba(255,255,255,0.45);
    display: flex;
    align-items: center;
  }

  .btn-icon, .btn-sync, .btn-upload {
    background: none;
    border: 1px solid rgba(255,255,255,0.15);
    color: rgba(255,255,255,0.6);
    border-radius: 6px;
    padding: 6px 12px;
    font-family: 'DM Mono', monospace;
    font-size: 0.7rem;
    cursor: pointer;
    transition: all 0.2s;
    letter-spacing: 0.05em;
  }
  .btn-icon:hover { background: rgba(255,255,255,0.1); color: white; }

  .btn-sync {
    background: var(--accent);
    border: none;
    color: white;
    font-size: 0.72rem;
    padding: 7px 14px;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .btn-sync:hover { background: var(--accent-light); }
  .btn-sync:disabled { opacity: 0.5; cursor: not-allowed; }

  .btn-upload {
    background: var(--book-accent);
    border: none;
    color: white;
    font-size: 0.72rem;
    padding: 7px 14px;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .btn-upload:hover { background: #5a7fb5; }

  #sync-drawer {
    grid-column: 1 / -1;
    background: #111;
    border-bottom: 2px solid var(--accent);
    padding: 12px 24px;
    font-family: 'DM Mono', monospace;
    font-size: 0.72rem;
    color: #aaa;
    max-height: 240px;
    overflow-y: auto;
    line-height: 1.6;
    white-space: pre-wrap;
  }
  #sync-drawer .ok   { color: #7ec87e; }
  #sync-drawer .err  { color: #e07e7e; }
  #sync-drawer .info { color: #7eaee0; }

  .device-dot {
    width: 7px; height: 7px;
    border-radius: 50%;
    display: inline-block;
    margin-right: 6px;
    background: #555;
    transition: background 0.5s;
  }
  .device-dot.connected { background: #7ec87e; box-shadow: 0 0 6px #7ec87e88; }

  .sidebar {
    background: var(--paper-dark);
    border-right: 1px solid var(--paper-border);
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }

  .tabs {
    display: flex;
    border-bottom: 1px solid var(--paper-border);
    background: var(--paper);
  }
  .tab {
    flex: 1;
    padding: 12px 8px;
    text-align: center;
    cursor: pointer;
    font-family: 'DM Mono', monospace;
    font-size: 0.72rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--text-muted);
    border-bottom: 2px solid transparent;
    transition: all 0.15s;
    user-select: none;
    position: relative;
  }
  .tab:hover { color: var(--text); }
  .tab.active {
    color: var(--text);
    border-bottom-color: var(--accent);
    background: var(--paper-dark);
  }
  .tab.active[data-tab="books"] { border-bottom-color: var(--book-accent); }
  .tab-count {
    background: rgba(0,0,0,0.07);
    font-size: 0.6rem;
    padding: 1px 6px;
    border-radius: 8px;
    margin-left: 4px;
  }

  .sidebar-actions {
    padding: 12px 16px 8px;
    border-bottom: 1px solid var(--paper-border);
    display: flex;
    gap: 8px;
    flex-direction: column;
  }
  .search-box {
    width: 100%;
    padding: 7px 12px;
    background: var(--paper);
    border: 1px solid var(--paper-border);
    border-radius: 6px;
    font-family: 'DM Sans', sans-serif;
    font-size: 0.78rem;
    color: var(--text);
    outline: none;
  }
  .search-box:focus { border-color: var(--accent-light); }

  .filter-chips {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
  }
  .chip {
    font-family: 'DM Mono', monospace;
    font-size: 0.62rem;
    padding: 3px 8px;
    background: var(--paper);
    border: 1px solid var(--paper-border);
    border-radius: 12px;
    cursor: pointer;
    color: var(--text-muted);
    user-select: none;
    transition: all 0.15s;
  }
  .chip:hover { background: var(--accent-pale); }
  .chip.active {
    background: var(--accent);
    border-color: var(--accent);
    color: white;
  }

  .item-list {
    flex: 1;
    overflow-y: auto;
    padding: 8px;
  }

  .nb-card {
    padding: 12px 14px;
    margin-bottom: 4px;
    border-radius: 8px;
    cursor: pointer;
    transition: all 0.15s;
    border: 1px solid transparent;
    position: relative;
    display: grid;
    grid-template-columns: 44px 1fr;
    gap: 10px;
    align-items: center;
  }
  .nb-card:hover { background: var(--paper); border-color: var(--paper-border); }
  .nb-card.active { background: var(--accent-pale); border-color: var(--accent-light); }
  .nb-card[data-kind="book"].active { background: var(--book-accent-pale); border-color: var(--book-accent); }
  .nb-card.active::before {
    content: '';
    position: absolute;
    left: 0; top: 20%; bottom: 20%;
    width: 3px;
    background: var(--accent);
    border-radius: 0 2px 2px 0;
  }
  .nb-card[data-kind="book"].active::before { background: var(--book-accent); }

  .nb-thumb {
    width: 44px; height: 56px;
    object-fit: cover;
    border-radius: 3px;
    background: white;
    box-shadow: 0 1px 3px var(--shadow);
  }
  .nb-thumb-placeholder {
    width: 44px; height: 56px;
    border-radius: 3px;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-muted);
    font-size: 0.55rem;
    font-family: 'DM Mono', monospace;
    text-align: center;
    padding: 4px;
    line-height: 1.1;
  }
  .nb-thumb-placeholder.notebook {
    background: var(--paper);
    border: 1px dashed var(--paper-border);
  }
  .nb-thumb-placeholder.book {
    background: linear-gradient(135deg, var(--book-accent), #6989b8);
    color: white;
    font-weight: 500;
    border: none;
  }

  .nb-info { min-width: 0; }
  .nb-name {
    font-size: 0.82rem;
    color: var(--text);
    margin-bottom: 4px;
    line-height: 1.3;
    word-break: break-word;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .nb-author {
    font-size: 0.68rem;
    color: var(--text-muted);
    font-style: italic;
    margin-bottom: 3px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .nb-meta {
    font-family: 'DM Mono', monospace;
    font-size: 0.62rem;
    color: var(--text-muted);
    display: flex;
    gap: 8px;
  }

  .nb-badge {
    display: inline-block;
    font-family: 'DM Mono', monospace;
    font-size: 0.58rem;
    padding: 1px 5px;
    border-radius: 3px;
    color: white;
    margin-bottom: 4px;
    letter-spacing: 0.04em;
  }
  .nb-badge.scribe { background: var(--notebook-accent); }
  .nb-badge.purchased { background: #6b7280; }
  .nb-badge.sideloaded { background: var(--book-accent); }

  .empty-state {
    padding: 32px 20px;
    text-align: center;
    color: var(--text-muted);
    font-size: 0.82rem;
    line-height: 1.6;
  }

  /* Reader pane */
  .reader {
    display: flex;
    flex-direction: column;
    overflow: hidden;
    background: var(--page-bg);
  }
  .reader-toolbar {
    background: var(--paper-dark);
    border-bottom: 1px solid var(--paper-border);
    padding: 10px 20px;
    display: flex;
    align-items: center;
    gap: 12px;
    flex-shrink: 0;
  }
  .reader-title {
    font-family: 'Playfair Display', serif;
    font-size: 0.95rem;
    color: var(--text);
    flex: 1;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .reader-title em {
    font-style: italic;
    color: var(--text-muted);
    font-size: 0.85rem;
  }

  .page-controls { display: flex; align-items: center; gap: 8px; }
  .btn-nav {
    width: 32px; height: 32px;
    display: flex; align-items: center; justify-content: center;
    background: var(--paper);
    border: 1px solid var(--paper-border);
    border-radius: 6px;
    cursor: pointer;
    color: var(--text);
    font-size: 1rem;
    user-select: none;
    transition: all 0.15s;
  }
  .btn-nav:hover:not(:disabled) {
    background: var(--accent-pale);
    border-color: var(--accent-light);
    color: var(--accent);
  }
  .btn-nav:disabled { opacity: 0.3; cursor: default; }
  .page-indicator {
    font-family: 'DM Mono', monospace;
    font-size: 0.72rem;
    color: var(--text-muted);
    min-width: 60px;
    text-align: center;
  }

  .zoom-controls {
    display: flex;
    align-items: center;
    gap: 6px;
    border-left: 1px solid var(--paper-border);
    padding-left: 12px;
  }
  .zoom-label {
    font-family: 'DM Mono', monospace;
    font-size: 0.68rem;
    color: var(--text-muted);
    min-width: 38px;
    text-align: center;
  }

  .page-viewport {
    flex: 1;
    overflow: auto;
    display: flex;
    align-items: flex-start;
    justify-content: center;
    padding: 32px;
    background: radial-gradient(ellipse at 50% 0%, rgba(180,160,130,0.08) 0%, transparent 70%), var(--page-bg);
    position: relative;
  }

  .page-wrapper {
    position: relative;
    transform-origin: top center;
    transition: transform 0.2s ease;
  }

  .page-shadow {
    box-shadow: 0 4px 16px var(--shadow), 0 1px 4px rgba(0,0,0,0.1), inset 0 0 0 1px rgba(0,0,0,0.06);
    border-radius: 2px;
  }

  .single-page {
    /* Container that JS sizes to fit the viewport; page-img fills it. */
    display: block;
    margin: 0 auto;
  }

  .page-img {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: contain;
    border-radius: 2px;
    background: white;
  }

  .pdf-iframe {
    width: 100%;
    height: 100%;
    border: none;
    background: white;
  }

  .loading-overlay {
    position: absolute;
    inset: 0;
    background: rgba(245,240,232,0.85);
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 16px;
    z-index: 10;
    backdrop-filter: blur(4px);
  }
  .spinner {
    width: 36px; height: 36px;
    border: 3px solid var(--paper-border);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .loading-text {
    font-family: 'DM Mono', monospace;
    font-size: 0.75rem;
    color: var(--text-muted);
  }
  .loading-sub {
    font-family: 'DM Sans', sans-serif;
    font-size: 0.7rem;
    color: var(--text-muted);
    opacity: 0.7;
    max-width: 320px;
    text-align: center;
    line-height: 1.5;
  }

  .welcome {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 48px;
    text-align: center;
    gap: 16px;
  }
  .welcome-icon { width: 72px; height: 72px; opacity: 0.2; }
  .welcome h2 {
    font-family: 'Playfair Display', serif;
    font-size: 1.6rem;
    color: var(--text);
    font-weight: 400;
  }
  .welcome p {
    color: var(--text-muted);
    font-size: 0.85rem;
    line-height: 1.7;
    max-width: 380px;
  }

  .thumb-strip {
    display: flex;
    gap: 8px;
    padding: 10px 16px;
    background: var(--paper-dark);
    border-top: 1px solid var(--paper-border);
    overflow-x: auto;
    flex-shrink: 0;
    scrollbar-width: thin;
    scrollbar-color: var(--paper-border) transparent;
  }
  .thumb {
    flex-shrink: 0;
    width: 52px;
    height: 68px;
    object-fit: cover;
    border-radius: 3px;
    border: 2px solid transparent;
    cursor: pointer;
    opacity: 0.6;
    transition: all 0.15s;
    background: white;
  }
  .thumb:hover { opacity: 0.9; transform: translateY(-2px); }
  .thumb.active {
    border-color: var(--accent);
    opacity: 1;
    box-shadow: 0 2px 8px var(--shadow);
  }

  .error-box, .info-box {
    margin: 32px auto;
    max-width: 480px;
    padding: 24px;
    border-radius: 8px;
    font-size: 0.82rem;
    line-height: 1.6;
  }
  .error-box {
    background: #fff5f5;
    border: 1px solid #f5c6c6;
    color: #8b3a3a;
  }
  .info-box {
    background: var(--paper-dark);
    border: 1px solid var(--paper-border);
    color: var(--text);
  }
  .error-box strong, .info-box strong { display: block; margin-bottom: 8px; font-size: 0.9rem; }

  .download-link {
    display: inline-block;
    margin-top: 12px;
    padding: 8px 16px;
    background: var(--book-accent);
    color: white;
    text-decoration: none;
    border-radius: 6px;
    font-family: 'DM Mono', monospace;
    font-size: 0.75rem;
  }
  .download-link:hover { background: #5a7fb5; }

  .warn-banner {
    grid-column: 1 / -1;
    background: #fef5e0;
    border-bottom: 1px solid #e5c87a;
    color: #6e4f1c;
    padding: 8px 24px;
    font-size: 0.78rem;
    font-family: 'DM Mono', monospace;
  }

  .kbd-hint {
    font-family: 'DM Mono', monospace;
    font-size: 0.62rem;
    color: var(--text-muted);
    opacity: 0.6;
    margin-left: 4px;
  }

  /* Upload modal */
  .modal-bg {
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.5);
    display: none;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }
  .modal-bg.show { display: flex; }
  .modal {
    background: var(--paper);
    border-radius: 12px;
    width: 480px;
    max-width: 90vw;
    padding: 24px;
    box-shadow: 0 8px 40px rgba(0,0,0,0.3);
  }
  .modal h3 {
    font-family: 'Playfair Display', serif;
    font-size: 1.2rem;
    margin-bottom: 4px;
  }
  .modal p { font-size: 0.82rem; color: var(--text-muted); margin-bottom: 16px; line-height: 1.5; }
  .drop-zone {
    border: 2px dashed var(--paper-border);
    border-radius: 8px;
    padding: 32px 24px;
    text-align: center;
    background: white;
    transition: all 0.15s;
    cursor: pointer;
  }
  .drop-zone:hover, .drop-zone.dragover {
    border-color: var(--book-accent);
    background: var(--book-accent-pale);
  }
  .drop-zone-text {
    font-family: 'DM Mono', monospace;
    font-size: 0.78rem;
    color: var(--text-muted);
    margin-bottom: 6px;
  }
  .drop-zone-hint {
    font-size: 0.72rem;
    color: var(--text-muted);
    opacity: 0.6;
  }
  .modal-actions {
    margin-top: 20px;
    display: flex;
    justify-content: flex-end;
    gap: 8px;
  }
  .btn-modal {
    padding: 8px 16px;
    border-radius: 6px;
    font-family: 'DM Mono', monospace;
    font-size: 0.72rem;
    cursor: pointer;
    border: 1px solid var(--paper-border);
    background: var(--paper);
    color: var(--text);
  }
  .btn-modal.primary {
    background: var(--book-accent);
    border-color: var(--book-accent);
    color: white;
  }
  .btn-modal:disabled { opacity: 0.5; cursor: not-allowed; }
  .upload-status {
    margin-top: 16px;
    font-family: 'DM Mono', monospace;
    font-size: 0.7rem;
    color: var(--text-muted);
    max-height: 120px;
    overflow-y: auto;
    line-height: 1.5;
    white-space: pre-wrap;
  }

  @media (max-width: 700px) {
    .shell { grid-template-columns: 1fr; grid-template-rows: 60px auto 1fr; }
    .sidebar { max-height: 280px; }
  }

  /* ── Zen mode ──────────────────────────────────────────────────────────── */
  /* Hide sidebar, topbar, thumb-strip; reveal a floating bottom toolbar */
  body.zen-mode .topbar,
  body.zen-mode .sidebar,
  body.zen-mode .thumb-strip,
  body.zen-mode .reader-toolbar {
    display: none !important;
  }
  body.zen-mode .shell {
    grid-template-columns: 1fr;
    grid-template-rows: 1fr;
  }
  body.zen-mode .reader { grid-column: 1; grid-row: 1; }
  body.zen-mode {
    background-image: none;
    background: var(--page-bg);
  }
  body.zen-mode .page-viewport {
    padding: 48px 32px;
  }

  .zen-controls {
    position: fixed;
    bottom: 24px;
    left: 50%;
    transform: translateX(-50%);
    display: none;
    align-items: center;
    gap: 12px;
    padding: 10px 16px;
    background: rgba(26, 20, 16, 0.85);
    border-radius: 24px;
    box-shadow: 0 4px 16px rgba(0,0,0,0.2);
    backdrop-filter: blur(8px);
    z-index: 200;
    opacity: 0;
    transition: opacity 0.25s ease;
    pointer-events: none;
    user-select: none;
  }
  body.zen-mode .zen-controls {
    display: flex;
  }
  body.zen-mode.show-zen-controls .zen-controls {
    opacity: 1;
    pointer-events: auto;
  }
  .zen-btn {
    width: 36px; height: 36px;
    border-radius: 50%;
    background: rgba(255,255,255,0.08);
    border: none;
    color: rgba(255,255,255,0.85);
    font-size: 1rem;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: background 0.15s;
  }
  .zen-btn:hover { background: rgba(255,255,255,0.18); }
  .zen-btn:disabled { opacity: 0.3; cursor: default; }
  .zen-page-indicator {
    font-family: 'DM Mono', monospace;
    font-size: 0.78rem;
    color: rgba(255,255,255,0.7);
    min-width: 60px;
    text-align: center;
  }
  .zen-exit {
    font-family: 'DM Mono', monospace;
    font-size: 0.65rem;
    letter-spacing: 0.08em;
    color: rgba(255,255,255,0.5);
    border-left: 1px solid rgba(255,255,255,0.15);
    padding-left: 12px;
    margin-left: 4px;
    cursor: pointer;
    text-transform: uppercase;
  }
  .zen-exit:hover { color: white; }
  .zen-divider {
    width: 1px;
    height: 22px;
    background: rgba(255,255,255,0.15);
    margin: 0 4px;
  }

  /* ── Book mode (two-page spread + flip animation) ──────────────────────── */

  .spread {
    display: flex;
    align-items: stretch;
    justify-content: center;
    gap: 0;
    perspective: 2400px;
    transform-origin: center center;
  }
  .spread .spread-page {
    position: relative;
    background: white;
    box-shadow: 0 4px 18px var(--shadow), 0 1px 4px rgba(0,0,0,0.08);
    overflow: hidden;
    flex: 0 0 auto;
    transition: box-shadow 0.3s;
  }
  .spread .spread-page img {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: contain;
    background: white;
  }
  /* The gutter where the two pages meet — cover-on-right means the spread
     can have a single page (cover or back). When two pages, they sit edge-to-edge
     with a subtle inner shadow simulating the binding. */
  .spread .spread-page.left {
    box-shadow: -3px 4px 18px var(--shadow), inset -8px 0 12px -8px rgba(0,0,0,0.18);
    border-radius: 2px 0 0 2px;
  }
  .spread .spread-page.right {
    box-shadow: 3px 4px 18px var(--shadow), inset 8px 0 12px -8px rgba(0,0,0,0.18);
    border-radius: 0 2px 2px 0;
  }
  .spread .spread-page.solo {
    border-radius: 2px;
    box-shadow: 0 4px 18px var(--shadow), 0 1px 4px rgba(0,0,0,0.08);
  }

  /* The flipping page — positioned absolutely on top of the spread during a
     transition. We swap the visible faces via backface-visibility. */
  .flip-page {
    position: absolute;
    top: 0;
    bottom: 0;
    transform-style: preserve-3d;
    transition: transform 0.6s cubic-bezier(0.4, 0.05, 0.3, 1);
    transform-origin: left center;
    z-index: 5;
    pointer-events: none;
  }
  .flip-page.flipping-forward {
    /* When turning to the NEXT spread, the right page (currently visible)
       rotates from 0 to -180deg around its left edge */
    transform: rotateY(-180deg);
  }
  .flip-page.flip-back-origin {
    /* When turning to the PREVIOUS spread, the page rotates the other way */
    transform-origin: right center;
  }
  .flip-page.flipping-backward {
    transform: rotateY(180deg);
  }
  .flip-face {
    position: absolute;
    inset: 0;
    backface-visibility: hidden;
    -webkit-backface-visibility: hidden;
    background: white;
    overflow: hidden;
  }
  .flip-face img {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: contain;
    background: white;
  }
  .flip-face.back {
    transform: rotateY(180deg);
  }

  /* Annotation status pill — appears in the reader toolbar for PDFs with
     a sidecar. Green when decoded successfully, amber when found but
     un-decodable. */
  .annotation-pill {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-family: 'DM Mono', monospace;
    font-size: 0.66rem;
    padding: 3px 9px;
    border-radius: 12px;
    margin-left: 8px;
    user-select: none;
  }
  .annotation-pill.ok {
    background: #d4e8d4;
    color: #2d5a2d;
    border: 1px solid #a5c8a5;
  }
  .annotation-pill.warn {
    background: #fef0d4;
    color: #6e4f1c;
    border: 1px solid #d4ba7a;
  }
  .annotation-pill.warn:hover { background: #fce5b8; }
  .annotation-pill span:first-child { font-size: 0.85rem; }

  /* Reader-mode toggle buttons in the toolbar */
  .mode-toggle {
    display: flex;
    align-items: center;
    gap: 4px;
    border-left: 1px solid var(--paper-border);
    padding-left: 12px;
    margin-left: 4px;
  }
  .mode-toggle .btn-nav.active {
    background: var(--accent);
    color: white;
    border-color: var(--accent);
  }
  .mode-toggle .btn-nav.active:hover {
    background: var(--accent-light);
  }

  /* ── Screenshots grid + lightbox ───────────────────────────────────────── */
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 8px;
    padding: 8px;
  }
  .screenshot-grid .ss-tile {
    position: relative;
    cursor: pointer;
    border-radius: 6px;
    overflow: hidden;
    background: var(--paper);
    border: 1px solid var(--paper-border);
    transition: all 0.15s;
    aspect-ratio: 0.75;  /* match Scribe page aspect */
  }
  .screenshot-grid .ss-tile:hover {
    transform: translateY(-2px);
    box-shadow: 0 4px 14px var(--shadow);
    border-color: var(--accent-light);
  }
  .screenshot-grid .ss-tile img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
  }
  .ss-tile .ss-meta {
    position: absolute;
    bottom: 0; left: 0; right: 0;
    background: linear-gradient(to top, rgba(0,0,0,0.7) 0%, transparent 100%);
    color: white;
    font-family: 'DM Mono', monospace;
    font-size: 0.62rem;
    padding: 16px 8px 6px 8px;
    line-height: 1.3;
  }

  /* Lightbox: takes over the reader pane when a screenshot is selected */
  .lightbox {
    position: relative;
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    background: var(--page-bg);
  }
  .lightbox-toolbar {
    background: var(--paper-dark);
    border-bottom: 1px solid var(--paper-border);
    padding: 10px 20px;
    display: flex;
    align-items: center;
    gap: 12px;
    flex-shrink: 0;
  }
  .lightbox-title {
    flex: 1;
    font-family: 'DM Mono', monospace;
    font-size: 0.78rem;
    color: var(--text);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .lightbox-meta {
    font-size: 0.7rem;
    color: var(--text-muted);
    margin-left: 8px;
  }
  .lightbox-image-wrap {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 32px;
    overflow: auto;
  }
  .lightbox-image {
    max-width: 100%;
    max-height: 100%;
    object-fit: contain;
    box-shadow: 0 4px 16px var(--shadow);
    border-radius: 2px;
    background: white;
  }
  .btn-danger {
    background: #c44;
    border: none;
    color: white;
    padding: 7px 14px;
    border-radius: 6px;
    font-family: 'DM Mono', monospace;
    font-size: 0.72rem;
    cursor: pointer;
    transition: background 0.15s;
  }
  .btn-danger:hover { background: #d55; }
  .btn-danger:disabled { opacity: 0.5; cursor: not-allowed; }

  /* Confirmation overlay shown over the lightbox before deleting */
  .delete-confirm {
    position: absolute;
    inset: 0;
    background: rgba(0,0,0,0.6);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 50;
  }
  .delete-confirm-card {
    background: white;
    padding: 24px;
    border-radius: 10px;
    max-width: 380px;
    box-shadow: 0 8px 32px rgba(0,0,0,0.3);
  }
  .delete-confirm-card h4 {
    font-family: 'Playfair Display', serif;
    font-size: 1.1rem;
    margin-bottom: 8px;
  }
  .delete-confirm-card p {
    font-size: 0.8rem;
    color: var(--text-muted);
    line-height: 1.5;
    margin-bottom: 16px;
  }
  .delete-confirm-actions {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
  }
  .delete-progress {
    margin-top: 12px;
    font-family: 'DM Mono', monospace;
    font-size: 0.7rem;
    color: var(--text-muted);
    line-height: 1.5;
    white-space: pre-wrap;
    max-height: 120px;
    overflow-y: auto;
  }
</style>
</head>
<body>
<div class="shell">

  <header class="topbar">
    <div class="logo">
      <svg class="logo-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
        <path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/>
        <path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/>
      </svg>
      Scribe Library
    </div>
    <button class="btn-sync" onclick="syncAll()">⇣ Sync All</button>
    <button class="btn-upload" onclick="showUploadModal()">⇡ Push to Kindle</button>
    <button class="btn-icon" onclick="refreshLibrary()">↻ Refresh</button>
    <button class="btn-icon" onclick="clearCache()">✕ Clear Cache</button>
    <div class="topbar-status">
      <span class="device-dot" id="device-dot"></span>
      <span id="status-text">Checking…</span>
    </div>
  </header>

  <div id="warn-banner" class="warn-banner" style="display:none"></div>

  <div id="sync-drawer" style="display:none">
    <div id="sync-log"></div>
  </div>

  <nav class="sidebar">
    <div class="tabs">
      <div class="tab active" data-tab="notebooks" onclick="switchTab('notebooks')">
        Notebooks <span class="tab-count" id="count-notebooks">0</span>
      </div>
      <div class="tab" data-tab="books" onclick="switchTab('books')">
        Books <span class="tab-count" id="count-books">0</span>
      </div>
      <div class="tab" data-tab="screenshots" onclick="switchTab('screenshots')">
        Screenshots <span class="tab-count" id="count-screenshots">0</span>
      </div>
    </div>

    <div class="sidebar-actions">
      <input class="search-box" type="text" placeholder="Filter…"
        oninput="setFilter(this.value)" id="search-input">
      <div class="filter-chips" id="filter-chips"></div>
    </div>

    <div class="item-list" id="item-list">
      <div class="empty-state">Loading…</div>
    </div>
  </nav>

  <main class="reader" id="reader">
    <div class="welcome">
      <svg class="welcome-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1">
        <path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/>
        <path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/>
      </svg>
      <h2>Choose something to read</h2>
      <p>Pick a notebook or book from the list. Use the tabs to switch between handwritten notes and your library.</p>
      <p style="font-family:'DM Mono',monospace;font-size:0.7rem;opacity:0.6">
        Once reading: <strong>z</strong> for zen mode · <strong>b</strong> for book mode · <strong>← →</strong> to flip pages
      </p>
    </div>
  </main>

</div>

<!-- Zen mode floating controls (visible only in zen mode, fade with mouse activity) -->
<div class="zen-controls" id="zen-controls">
  <button class="zen-btn" id="zen-prev" onclick="goPage(-1)" title="Previous page (←)">‹</button>
  <span class="zen-page-indicator" id="zen-indicator">1 / 1</span>
  <button class="zen-btn" id="zen-next" onclick="goPage(1)" title="Next page (→)">›</button>
  <span class="zen-divider"></span>
  <button class="zen-btn" onclick="changeZoom(-0.15)" title="Zoom out (−)">−</button>
  <span class="zen-page-indicator" id="zen-zoom-label" style="min-width:48px">100%</span>
  <button class="zen-btn" onclick="changeZoom(0.15)" title="Zoom in (+)">+</button>
  <button class="zen-btn" onclick="resetZoom()" title="Reset zoom (0)">⊡</button>
  <span class="zen-exit" onclick="toggleZen()" title="Exit zen mode (Esc)">Exit</span>
</div>

<!-- Upload modal -->
<div class="modal-bg" id="upload-modal">
  <div class="modal">
    <h3>Push to Kindle</h3>
    <p>Drop a PDF (or .epub, .mobi, .txt, .docx, .rtf) here to send it to your Kindle's documents folder via USB. Max 100MB. The file may take a minute or two to appear in the Kindle's home screen after upload.</p>
    <div class="drop-zone" id="drop-zone" onclick="document.getElementById('file-input').click()">
      <div class="drop-zone-text" id="drop-zone-text">Drop a file here, or click to browse</div>
      <div class="drop-zone-hint">PDF · EPUB · MOBI · TXT · DOCX · RTF</div>
    </div>
    <input type="file" id="file-input" style="display:none" accept=".pdf,.epub,.mobi,.txt,.docx,.rtf">
    <div class="upload-status" id="upload-status"></div>
    <div class="modal-actions">
      <button class="btn-modal" onclick="hideUploadModal()" id="btn-cancel">Close</button>
      <button class="btn-modal primary" onclick="doUpload()" id="btn-upload" disabled>Upload</button>
    </div>
  </div>
</div>

<script>
// ── State ────────────────────────────────────────────────────────────────────
let library = { notebooks: [], books: [], screenshots: [] };
let currentTab = 'notebooks';
let currentItem = null;
let currentPage = 0;
let totalPages = 0;
let zoom = 1.0;
let filterText = '';
let activeBookFilter = 'all';
let pendingUploadFile = null;
// Reader modes — bookMode persists across sessions, zen is per-session
let bookMode = false;
try {
  bookMode = window.localStorage && window.localStorage.getItem('bookMode') === '1';
} catch(e){}

// ── Init ─────────────────────────────────────────────────────────────────────
async function init() {
  await loadStatus();
  await loadLibrary();
  setInterval(loadStatus, 5000);
  document.addEventListener('keydown', handleKey);
  setupDropZone();

  // When the window resizes, recompute page dimensions so things still fit
  // the viewport. We touch only inline styles, not innerHTML, so images
  // don't reload. Throttled to avoid layout thrash.
  let resizeTimer = null;
  window.addEventListener('resize', () => {
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (currentItem) resizePageContainers();
    }, 150);
  });
}

async function loadStatus() {
  try {
    const r = await fetch('/api/status');
    const s = await r.json();
    const dot = document.getElementById('device-dot');
    const txt = document.getElementById('status-text');
    if (s.device_connected) {
      dot.classList.add('connected');
      txt.textContent = `${s.device_name} · ${s.notebook_count}nb · ${s.book_count}bk · ${s.screenshot_count}ss`;
    } else {
      dot.classList.remove('connected');
      txt.textContent = (s.notebook_count + s.book_count + s.screenshot_count > 0)
        ? `${s.notebook_count}nb · ${s.book_count}bk · ${s.screenshot_count}ss (offline)`
        : 'No Kindle connected';
    }
    const banner = document.getElementById('warn-banner');
    if (!s.converter_ready && s.converter_message) {
      banner.textContent = '⚠ ' + s.converter_message.split('\n')[0];
      banner.style.display = 'block';
    } else { banner.style.display = 'none'; }
  } catch(e) {
    document.getElementById('status-text').textContent = 'Server error';
  }
}

async function loadLibrary() {
  try {
    const r = await fetch('/api/library');
    library = await r.json();
    if (!library.screenshots) library.screenshots = [];
    document.getElementById('count-notebooks').textContent = library.notebooks.length;
    document.getElementById('count-books').textContent = library.books.length;
    document.getElementById('count-screenshots').textContent = library.screenshots.length;
    renderList();
  } catch(e) {
    document.getElementById('item-list').innerHTML =
      `<div class="empty-state">Failed to load library: ${escHtml(e.message)}</div>`;
  }
}

// ── Tabs & filtering ─────────────────────────────────────────────────────────
function switchTab(tab) {
  currentTab = tab;
  document.querySelectorAll('.tab').forEach(el => {
    el.classList.toggle('active', el.dataset.tab === tab);
  });
  renderFilterChips();
  renderList();
}

function renderFilterChips() {
  const wrap = document.getElementById('filter-chips');
  if (currentTab === 'books') {
    wrap.innerHTML = `
      <span class="chip ${activeBookFilter==='all'?'active':''}" onclick="setBookFilter('all')">All</span>
      <span class="chip ${activeBookFilter==='purchased'?'active':''}" onclick="setBookFilter('purchased')">Purchased</span>
      <span class="chip ${activeBookFilter==='sideloaded'?'active':''}" onclick="setBookFilter('sideloaded')">Sideloaded</span>
    `;
  } else {
    wrap.innerHTML = '';
  }
}

function setBookFilter(f) { activeBookFilter = f; renderFilterChips(); renderList(); }
function setFilter(v) { filterText = v; renderList(); }

function currentItems() {
  let items;
  if (currentTab === 'notebooks') items = library.notebooks;
  else if (currentTab === 'books') items = library.books;
  else items = library.screenshots || [];

  const t = filterText.toLowerCase();
  return items
    .filter(it => !t ||
      (it.name||'').toLowerCase().includes(t) ||
      (it.title||'').toLowerCase().includes(t) ||
      (it.filename||'').toLowerCase().includes(t) ||
      (it.authors||[]).some(a => a.toLowerCase().includes(t)))
    .filter(it => currentTab !== 'books' || activeBookFilter === 'all' ||
                   it.source === activeBookFilter);
}

// ── List rendering ───────────────────────────────────────────────────────────
function renderList() {
  const list = document.getElementById('item-list');
  const items = currentItems();

  if (!items.length) {
    let all;
    if (currentTab === 'notebooks') all = library.notebooks;
    else if (currentTab === 'books') all = library.books;
    else all = library.screenshots || [];
    list.innerHTML = all.length === 0
      ? `<div class="empty-state">No ${currentTab} found.<br><br>
          Click <strong>Sync All</strong> above to pull from Kindle.</div>`
      : `<div class="empty-state">No matches for "${escHtml(filterText)}"</div>`;
    return;
  }

  // Screenshots use a square grid layout, not the row cards
  if (currentTab === 'screenshots') {
    list.innerHTML = `<div class="screenshot-grid">${
      items.map(s => renderScreenshotTile(s)).join('')
    }</div>`;
    return;
  }

  list.innerHTML = items.map(it => renderCard(it)).join('');
}

function renderScreenshotTile(s) {
  const isActive = currentItem?.id === s.id;
  const dateLabel = formatDateTime(s.captured_at || s.modified);
  return `
    <div class="ss-tile ${isActive?'active':''}" onclick="openItem('${s.id}')">
      <img src="/screenshot/${s.id}" alt="${escHtml(s.filename)}" loading="lazy">
      <div class="ss-meta">${dateLabel}</div>
    </div>`;
}

function formatDateTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  // E.g. "Apr 29, 10:30 AM"
  return d.toLocaleString('en-US', {
    month:'short', day:'numeric',
    hour:'numeric', minute:'2-digit',
  });
}

function renderCard(it) {
  const isActive = currentItem?.id === it.id;
  const thumbHtml = renderThumb(it);
  const badge = renderBadge(it);
  const author = (it.authors && it.authors.length)
    ? `<div class="nb-author">${escHtml(it.authors.join(', '))}</div>` : '';
  return `
    <div class="nb-card ${isActive?'active':''}" data-kind="${it.kind}"
         onclick="openItem('${it.id}')">
      ${thumbHtml}
      <div class="nb-info">
        ${badge}
        <div class="nb-name">${formatName(it)}</div>
        ${author}
        <div class="nb-meta">
          <span>${it.size_mb}mb</span>
          <span>${formatDate(it.modified)}</span>
          ${it.format ? `<span>${escHtml(it.format)}</span>` : ''}
        </div>
      </div>
    </div>`;
}

function renderThumb(it) {
  if (it.kind === 'notebook' && it.has_thumbnail) {
    return `<img class="nb-thumb" src="/thumbnail/${it.id}" alt="">`;
  }
  if (it.kind === 'book' && it.has_cover) {
    return `<img class="nb-thumb" src="/cover/${it.id}" alt="" onerror="this.style.display='none'">`;
  }
  if (it.kind === 'notebook') {
    return `<div class="nb-thumb-placeholder notebook">.nbk</div>`;
  }
  // Book without cover yet — show a colored tile with the format
  return `<div class="nb-thumb-placeholder book">${escHtml(it.format || 'BOOK')}</div>`;
}

function renderBadge(it) {
  if (it.kind === 'notebook')
    return '<span class="nb-badge scribe">Scribe</span>';
  if (it.source === 'purchased')
    return '<span class="nb-badge purchased">Purchased</span>';
  return '<span class="nb-badge sideloaded">Sideloaded</span>';
}

function formatName(it) {
  const name = it.title || it.name;
  // For notebooks with UUID names, show abbreviated
  if (it.kind === 'notebook' && /^[a-f0-9]{8}-/.test(name)) {
    return `Notebook <span style="opacity:0.4;font-size:0.85em">#${name.slice(0,8)}</span>`;
  }
  return escHtml(name);
}

function formatDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return d.toLocaleDateString('en-US', {month:'short', day:'numeric'});
}

function escHtml(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── Open + read ──────────────────────────────────────────────────────────────
async function openItem(id) {
  // Find across all three lists
  const allItems = [
    ...library.notebooks,
    ...library.books,
    ...(library.screenshots || []),
  ];
  const it = allItems.find(i => i.id === id);
  if (!it) return;
  currentItem = it;
  currentPage = 0;
  // Don't carry zen mode across item switches — that would feel like a trap
  if (document.body.classList.contains('zen-mode')) {
    document.body.classList.remove('zen-mode');
    unbindZenHover();
  }
  renderList();

  // Screenshots get a dedicated lightbox view, not the paged reader
  if (it.kind === 'screenshot') {
    showScreenshot(it);
    return;
  }

  const reader = document.getElementById('reader');
  reader.innerHTML = `
    <div class="reader-toolbar">
      <div class="reader-title">${formatName(it)}</div>
    </div>
    <div class="page-viewport" style="position:relative">
      <div class="loading-overlay">
        <div class="spinner"></div>
        <div class="loading-text">${it.kind === 'notebook' ? 'Decoding handwriting…' : 'Loading book…'}</div>
        <div class="loading-sub">${it.kind === 'notebook'
          ? 'First open of a notebook takes a few seconds — strokes are being rendered. Cached for next time.'
          : 'Extracting metadata and preparing pages.'}</div>
      </div>
    </div>
  `;

  const endpoint = it.kind === 'notebook'
    ? `/api/notebook/${id}` : `/api/book/${id}`;

  try {
    const r = await fetch(endpoint);
    if (!r.ok) {
      const err = await r.json();
      throw new Error(err.error || `HTTP ${r.status}`);
    }
    const data = await r.json();

    if (data.viewable === false) {
      showRawDownload(it, data);
      return;
    }

    if (data.fallback_pdf) {
      // Page extraction didn't work — show the PDF in an iframe
      showPdfFallback(it, data);
      return;
    }

    if (!data.pages || data.pages.length === 0) {
      showEmptyMessage(it);
      return;
    }

    buildPagedReader(it, data);

    // For sideloaded PDFs, check whether there's an annotation sidecar and
    // surface the result in the toolbar. Done after buildPagedReader so it
    // can inject into the existing toolbar without blocking page render.
    if (it.kind === 'book' && it.extension === '.pdf') {
      checkAnnotationStatus(it);
    }
  } catch(e) {
    showError(it, e.message);
  }
}

async function checkAnnotationStatus(it) {
  try {
    const r = await fetch(`/api/annotations/${it.id}`);
    const data = await r.json();
    const toolbar = document.querySelector('.reader-toolbar');
    if (!toolbar) return;

    // Already have a status pill from a previous render? Remove it.
    const existing = toolbar.querySelector('.annotation-pill');
    if (existing) existing.remove();

    if (!data.has_sidecar) {
      // Don't surface anything — most PDFs don't have annotations and
      // showing "no annotations found" on every PDF is noise.
      return;
    }

    const pill = document.createElement('span');
    pill.className = 'annotation-pill';
    if (r.status === 200 && !data.error) {
      const n = data.pages_with_strokes ? data.pages_with_strokes.length : 0;
      pill.classList.add('ok');
      pill.title = `Strokes detected on ${n} page(s). They've been baked into the rendered PDF.`;
      pill.innerHTML = `<span>✏</span> ${n} annotated page${n!==1?'s':''}`;
    } else {
      pill.classList.add('warn');
      pill.title = `Sidecar found but couldn't decode strokes. Click for details.`;
      pill.innerHTML = `<span>⚠</span> annotations not decoded`;
      pill.style.cursor = 'pointer';
      pill.onclick = () => showAnnotationDebug(it.id, data);
    }
    // Insert just after the title
    const title = toolbar.querySelector('.reader-title');
    if (title && title.nextSibling) {
      toolbar.insertBefore(pill, title.nextSibling);
    } else {
      toolbar.appendChild(pill);
    }
  } catch (e) {
    // Silent — debug endpoint failure shouldn't break the reader
    console.warn('Annotation check failed:', e);
  }
}

function showAnnotationDebug(id, data) {
  const lines = [];
  lines.push(`Sidecar: ${data.sidecar_dir || '(unknown)'}`);
  lines.push('');
  if (data.error) {
    lines.push(`Error (${data.step || 'unknown step'}):`);
    lines.push(`  ${data.error}`);
    if (data.details) {
      lines.push('');
      lines.push('Details:');
      lines.push(data.details);
    }
  }
  if (data.sidecar_files && data.sidecar_files.length) {
    lines.push('');
    lines.push('Files in sidecar:');
    for (const f of data.sidecar_files) {
      lines.push(`  ${f.name} (${f.size} bytes)`);
    }
  }
  if (data.log && data.log.length) {
    lines.push('');
    lines.push('Decoder log:');
    for (const line of data.log) lines.push(`  ${line}`);
  }
  alert(lines.join('\n'));
}

function buildPagedReader(it, data) {
  totalPages = data.pages.length;
  currentPage = 0;
  window._pages = data.pages;

  // Book mode is only allowed for items with at least 3 pages and not for
  // raw PDFs (data.fallback_pdf would have been true for those anyway).
  const bookModeAllowed = totalPages >= 3;
  // Persisted preference — but we won't enable it on items where it's not allowed
  if (!bookModeAllowed) bookMode = false;

  const reader = document.getElementById('reader');
  reader.innerHTML = `
    <div class="reader-toolbar">
      <div class="reader-title">
        ${formatName(it)}
        <em style="margin-left:8px">· ${totalPages} page${totalPages!==1?'s':''}</em>
      </div>
      <div class="page-controls">
        <button class="btn-nav" id="btn-prev" onclick="goPage(-1)" title="Previous (←)">‹</button>
        <span class="page-indicator" id="page-indicator">1 / ${totalPages}</span>
        <button class="btn-nav" id="btn-next" onclick="goPage(1)" title="Next (→)">›</button>
      </div>
      <div class="zoom-controls">
        <button class="btn-nav" onclick="changeZoom(-0.15)">−</button>
        <span class="zoom-label" id="zoom-label">100%</span>
        <button class="btn-nav" onclick="changeZoom(0.15)">+</button>
        <button class="btn-nav" onclick="resetZoom()">⊡</button>
      </div>
      <div class="mode-toggle">
        <button class="btn-nav ${bookMode?'active':''}" onclick="toggleBookMode()"
                ${bookModeAllowed?'':'disabled'}
                title="${bookModeAllowed?'Book mode (b)':'Need 3+ pages for book mode'}">⛶</button>
        <button class="btn-nav" onclick="toggleZen()" title="Zen mode (z)">☯</button>
      </div>
      <a class="btn-icon" style="color:var(--text);border-color:var(--paper-border)"
         href="${data.pdf_url}" target="_blank" rel="noopener">⤓ PDF</a>
      <span class="kbd-hint">← → · z · b</span>
    </div>
    <div class="page-viewport" id="page-viewport">
      <div class="page-wrapper" id="page-wrapper">
        <!-- Content rendered by renderPage() based on bookMode -->
      </div>
    </div>
    <div class="thumb-strip" id="thumb-strip">
      ${data.pages.map((p, i) => `
        <img class="thumb ${i===0?'active':''}" src="${p}" loading="lazy"
             onclick="jumpPage(${i})" id="thumb-${i}">
      `).join('')}
    </div>
  `;
  // Apply book mode class on body so global CSS hides the thumb strip
  document.body.classList.toggle('book-mode', bookMode);
  showPage(0);
}

function showPage(n) {
  if (n < 0 || n >= totalPages) return;

  // In book mode, snap n to even-when-paired layout (cover-on-right model):
  //  spread 0: page 0 alone (cover, on right)
  //  spread 1: pages [1, 2]
  //  spread 2: pages [3, 4]
  //  spread k (k>=1): pages [2k-1, 2k]
  if (bookMode) {
    n = snapToSpreadStart(n);
  }

  const oldPage = currentPage;
  currentPage = n;
  updateChrome();

  // Decide whether to animate (book mode + adjacent spread) or just swap
  const shouldAnimate = bookMode && shouldFlipAnimate(oldPage, n);
  if (shouldAnimate) {
    animateFlip(oldPage, n);
  } else {
    renderPage();
  }
}

function isCoverOnRight() {
  // Books (purchased/sideloaded) follow the codex convention: page 0 is a
  // cover that sits alone on the right of the first spread.
  // Notebooks don't have a "cover" — they're filled from page 1 — so they
  // pair from the start: [0,1], [2,3], [4,5]…
  return currentItem && currentItem.kind === 'book';
}

function snapToSpreadStart(n) {
  // Return the leftmost page index of the spread containing page n.
  if (isCoverOnRight()) {
    // Books: spread 0 = [_, 0], spread k>=1 = [2k-1, 2k]
    if (n <= 0) return 0;
    return n % 2 === 1 ? n : n - 1;
  }
  // Notebooks: spread k = [2k, 2k+1]
  return n - (n % 2);
}

function spreadPagesFor(n) {
  // Return [leftPageIndex|null, rightPageIndex|null] for the spread starting at n.
  if (isCoverOnRight()) {
    if (n === 0) return [null, 0];
    return [n, (n + 1 < totalPages) ? n + 1 : null];
  }
  // Notebooks: simple paired layout, no solo cover
  return [n, (n + 1 < totalPages) ? n + 1 : null];
}

function spreadIndexOf(n) {
  // Map a page index to a 0-based spread number.
  if (isCoverOnRight()) {
    return n === 0 ? 0 : Math.floor((n + 1) / 2);
  }
  return Math.floor(n / 2);
}

function shouldFlipAnimate(oldN, newN) {
  if (oldN === newN) return false;
  // Animation looks wrong when the wrapper is scaled (zoom != 1) because the
  // flip-page is positioned in untransformed coords. Skip animation in that
  // case — the spread still renders, just without the flip transition.
  if (Math.abs(zoom - 1.0) > 0.01) return false;
  // Animate only when moving to an adjacent spread; jumping farther just snaps.
  if (Math.abs(spreadIndexOf(oldN) - spreadIndexOf(newN)) !== 1) return false;
  // Skip animation when either the leaving or arriving spread is solo
  // (cover-on-right or trailing single page) — the gutter math doesn't apply.
  const oldSolo = spreadPagesFor(snapToSpreadStart(oldN)).filter(x => x !== null).length === 1;
  const newSolo = spreadPagesFor(snapToSpreadStart(newN)).filter(x => x !== null).length === 1;
  if (oldSolo || newSolo) return false;
  return true;
}

function renderPage() {
  const wrapper = document.getElementById('page-wrapper');
  if (!wrapper) return;

  if (bookMode) {
    const [left, right] = spreadPagesFor(currentPage);
    wrapper.innerHTML = renderSpreadHtml(left, right);
  } else {
    // Single-page mode uses the same fit-to-viewport sizing as book mode so
    // 100% means "page fills the available space without scrolling" — not
    // "natural pixel size of the rasterized image" (which is way bigger).
    const url = window._pages[currentPage];
    wrapper.innerHTML = `
      <div class="single-page" id="single-page">
        <img class="page-img page-shadow" id="page-img"
             src="${url}" alt="Page" draggable="false">
      </div>
    `;
  }
  resizePageContainers();
  applyZoom();
}

function resizePageContainers() {
  // Recompute and apply the per-page pixel dimensions. Called on initial
  // render AND on window resize — touches only the .style of existing
  // elements, never the innerHTML, so images don't reload.
  if (bookMode) {
    const size = pageSize(/*paired=*/true);
    document.querySelectorAll('.spread-page').forEach(el => {
      el.style.cssText = size;
    });
  } else {
    const single = document.getElementById('single-page');
    if (single) single.style.cssText = pageSize(/*paired=*/false);
  }
}

function renderSpreadHtml(leftIdx, rightIdx) {
  // Two pages side by side, sized to fit the viewport.
  // .solo is for single-page spreads (cover on its own, or a final lone page).
  const isSolo = (leftIdx === null) !== (rightIdx === null);
  const size = pageSize(/*paired=*/true);
  if (isSolo) {
    const idx = leftIdx ?? rightIdx;
    const url = window._pages[idx];
    return `
      <div class="spread">
        <div class="spread-page solo" id="spread-solo" style="${size}">
          <img src="${url}" alt="Page ${idx+1}">
        </div>
      </div>`;
  }
  return `
    <div class="spread">
      <div class="spread-page left" id="spread-left" style="${size}">
        <img src="${window._pages[leftIdx]}" alt="Page ${leftIdx+1}">
      </div>
      <div class="spread-page right" id="spread-right" style="${size}">
        <img src="${window._pages[rightIdx]}" alt="Page ${rightIdx+1}">
      </div>
    </div>`;
}

function pageSize(paired) {
  // Compute pixel dimensions for ONE page that:
  //   - preserves the Scribe aspect ratio (1860:2480 ≈ 0.75)
  //   - fits inside the page-viewport with comfortable padding
  //   - allows the requested layout: paired=true means two pages side-by-side
  //     (so one page gets half the viewport width); paired=false means a
  //     single page can use the full viewport width.
  const viewport = document.getElementById('page-viewport');
  if (!viewport) return 'width:400px;height:560px';
  const padding = 64;  // total horizontal/vertical padding allowance
  const availH = viewport.clientHeight - padding;
  const availW = paired
    ? (viewport.clientWidth - padding) / 2
    : (viewport.clientWidth - padding);
  const aspect = 1860 / 2480;  // width:height
  // Start from the height limit and clamp by width
  let h = availH;
  let w = h * aspect;
  if (w > availW) {
    w = availW;
    h = w / aspect;
  }
  return `width:${w}px;height:${h}px`;
}

// Keep the old name as an alias so animateFlip's call site stays working.
function spreadPageSize() {
  return pageSize(/*paired=*/true);
}

function animateFlip(oldN, newN) {
  // Build a flip-page overlay: a page-sized element rotating around the
  // gutter, with TWO faces — front face shows the page leaving, back face
  // shows the page coming in. CSS `backface-visibility: hidden` swaps which
  // is visible halfway through the rotation.
  const forward = newN > oldN;
  const wrapper = document.getElementById('page-wrapper');
  if (!wrapper) { renderPage(); return; }

  // Determine which page leaves and which arrives
  // Forward: from old spread's right page → next spread's left page
  // Backward: from old spread's left page → previous spread's right page
  let leavingIdx, arrivingIdx;
  if (forward) {
    const oldSpread = spreadPagesFor(oldN);
    leavingIdx = oldSpread[1] !== null ? oldSpread[1] : oldSpread[0];
    const newSpread = spreadPagesFor(newN);
    arrivingIdx = newSpread[0] !== null ? newSpread[0] : newSpread[1];
  } else {
    const oldSpread = spreadPagesFor(oldN);
    leavingIdx = oldSpread[0] !== null ? oldSpread[0] : oldSpread[1];
    const newSpread = spreadPagesFor(newN);
    arrivingIdx = newSpread[1] !== null ? newSpread[1] : newSpread[0];
  }

  if (leavingIdx === null || arrivingIdx === null) {
    renderPage();
    return;
  }

  // First, render the destination spread underneath
  renderPage();
  // The flip-page overlay sits on top and animates
  const flipPage = document.createElement('div');
  flipPage.className = 'flip-page' + (forward ? '' : ' flip-back-origin');
  flipPage.style.cssText = spreadPageSize();
  // Position: forward = on the right side of gutter; backward = on the left
  const spreadEl = wrapper.querySelector('.spread');
  if (!spreadEl) { return; }
  const rect = spreadEl.getBoundingClientRect();
  const wrapperRect = wrapper.getBoundingClientRect();
  // Place the flip page over the soon-to-be-gone page
  const pageW = rect.width / 2;
  flipPage.style.left = forward
    ? `${rect.left - wrapperRect.left + pageW}px`
    : `${rect.left - wrapperRect.left}px`;
  flipPage.style.top = `${rect.top - wrapperRect.top}px`;

  flipPage.innerHTML = `
    <div class="flip-face front"><img src="${window._pages[leavingIdx]}" alt=""></div>
    <div class="flip-face back"><img src="${window._pages[arrivingIdx]}" alt=""></div>
  `;
  wrapper.appendChild(flipPage);

  // Force a reflow so the transition fires
  void flipPage.offsetWidth;
  flipPage.classList.add(forward ? 'flipping-forward' : 'flipping-backward');

  // Clean up after the transition
  setTimeout(() => {
    if (flipPage.parentNode) flipPage.parentNode.removeChild(flipPage);
  }, 650);
}

function updateChrome() {
  // Compute the visible page range for the indicator. In book mode, a
  // spread shows two pages so we display "1–2 / 7"; the cover spread on a
  // book shows just "1 / 7" (cover alone).
  function pageRangeText() {
    if (!bookMode) {
      return `${currentPage + 1} / ${totalPages}`;
    }
    const [left, right] = spreadPagesFor(currentPage);
    const visible = [left, right].filter(x => x !== null);
    if (visible.length === 1) {
      return `${visible[0] + 1} / ${totalPages}`;
    }
    return `${visible[0] + 1}–${visible[1] + 1} / ${totalPages}`;
  }

  // The "next" button should be disabled when we're already on the last
  // spread. Determining "last spread" depends on whether we have a solo
  // cover. Easier: check whether goPage(+1) would actually advance.
  function isAtFirstSpread() {
    return currentPage === 0;
  }
  function isAtLastSpread() {
    if (!bookMode) return currentPage >= totalPages - 1;
    const [left, right] = spreadPagesFor(currentPage);
    const lastVisible = right !== null ? right : left;
    return lastVisible >= totalPages - 1;
  }

  // Top toolbar
  const ind = document.getElementById('page-indicator');
  if (ind) ind.textContent = pageRangeText();
  const prev = document.getElementById('btn-prev');
  const next = document.getElementById('btn-next');
  if (prev) prev.disabled = isAtFirstSpread();
  if (next) next.disabled = isAtLastSpread();

  // Zen-mode floating controls
  const zenInd = document.getElementById('zen-indicator');
  if (zenInd) zenInd.textContent = pageRangeText();
  const zenPrev = document.getElementById('zen-prev');
  const zenNext = document.getElementById('zen-next');
  if (zenPrev) zenPrev.disabled = isAtFirstSpread();
  if (zenNext) zenNext.disabled = isAtLastSpread();

  // Thumb-strip active state. In book mode a spread shows up to two pages,
  // so both of them get the .active class (the "single page vibe" — every
  // visible page is highlighted in the strip, not just the spread's start).
  const visiblePages = bookMode
    ? spreadPagesFor(currentPage).filter(x => x !== null)
    : [currentPage];
  const visibleSet = new Set(visiblePages);
  document.querySelectorAll('.thumb').forEach((el, i) => {
    el.classList.toggle('active', visibleSet.has(i));
  });
  // Scroll the leftmost visible page into view so we don't keep yanking
  // the strip when navigating forward across spreads.
  const scrollTarget = visiblePages[0];
  const t = document.getElementById(`thumb-${scrollTarget}`);
  if (t) t.scrollIntoView({behavior:'smooth', inline:'center', block:'nearest'});
}

function goPage(delta) {
  // In single-page mode, just step by 1.
  if (!bookMode) {
    showPage(currentPage + delta);
    return;
  }

  // In book mode, navigation moves to the adjacent SPREAD. The increment
  // depends on whether this item has a solo cover.
  if (isCoverOnRight()) {
    // Books: cover (0) is alone on the right, then [1,2], [3,4]…
    if (delta > 0) {
      if (currentPage === 0) showPage(1);
      else showPage(currentPage + 2);
    } else {
      if (currentPage <= 2) showPage(0);
      else showPage(currentPage - 2);
    }
  } else {
    // Notebooks: paired layout from page 0, no solo cover.
    if (delta > 0) {
      showPage(currentPage + 2);
    } else {
      showPage(Math.max(0, currentPage - 2));
    }
  }
}

function jumpPage(n) {
  // Thumbnail click: if book mode is active, snap to the containing spread
  showPage(n);
}

function changeZoom(d) { zoom = Math.max(0.3, Math.min(3.0, zoom + d)); applyZoom(); }
function resetZoom() { zoom = 1.0; applyZoom(); }
function applyZoom() {
  const w = document.getElementById('page-wrapper');
  const l = document.getElementById('zoom-label');
  if (w) w.style.transform = `scale(${zoom})`;
  if (l) l.textContent = Math.round(zoom * 100) + '%';
  // Also update the zen-mode zoom indicator if it exists
  const zenLabel = document.getElementById('zen-zoom-label');
  if (zenLabel) zenLabel.textContent = Math.round(zoom * 100) + '%';
}

function showPdfFallback(it, data) {
  const reader = document.getElementById('reader');
  reader.innerHTML = `
    <div class="reader-toolbar">
      <div class="reader-title">${formatName(it)} <em style="margin-left:8px">· PDF</em></div>
      <a class="btn-icon" style="color:var(--text);border-color:var(--paper-border)"
         href="${data.pdf_url}" target="_blank" rel="noopener">⤓ Download</a>
    </div>
    <iframe class="pdf-iframe" src="${data.pdf_url}#view=FitH"></iframe>
  `;
}

function showRawDownload(it, data) {
  const reader = document.getElementById('reader');
  reader.innerHTML = `
    <div class="reader-toolbar">
      <div class="reader-title">${formatName(it)}</div>
    </div>
    <div class="page-viewport">
      <div class="info-box">
        <strong>${escHtml(it.format)} books open in another app</strong>
        ${escHtml(data.message || 'In-browser viewing is not yet supported for this format.')}
        <br>
        <a class="download-link" href="${data.download_url}">⤓ Download original</a>
      </div>
    </div>
  `;
}

function showEmptyMessage(it) {
  document.getElementById('reader').innerHTML = `
    <div class="page-viewport">
      <div class="info-box">
        <strong>No pages found</strong>
        <em>${formatName(it)}</em> was processed, but no pages came out.
        It may be empty, or the format isn't fully supported yet.
      </div>
    </div>`;
}

function showError(it, msg) {
  document.getElementById('reader').innerHTML = `
    <div class="page-viewport">
      <div class="error-box">
        <strong>Could not open this item</strong>
        <pre style="white-space:pre-wrap;font-family:'DM Mono',monospace;font-size:0.75rem;line-height:1.5;margin:8px 0;color:#6e3030">${escHtml(msg)}</pre>
        Check the server console for the full traceback.
      </div>
    </div>`;
}

// ── Screenshot lightbox ──────────────────────────────────────────────────────
function showScreenshot(it) {
  const dateLabel = formatDateTime(it.captured_at || it.modified);
  const reader = document.getElementById('reader');
  reader.innerHTML = `
    <div class="lightbox" id="lightbox">
      <div class="lightbox-toolbar">
        <div class="lightbox-title">${escHtml(it.filename)}</div>
        <span class="lightbox-meta">${dateLabel} · ${it.size_kb}kb</span>
        <a class="btn-icon" style="color:var(--text);border-color:var(--paper-border)"
           href="/screenshot/${it.id}" download="${escHtml(it.filename)}">⤓ Download</a>
        <button class="btn-danger" onclick="confirmDeleteScreenshot('${it.id}')"
                title="Delete from local AND from Kindle">🗑 Delete</button>
      </div>
      <div class="lightbox-image-wrap">
        <img class="lightbox-image" src="/screenshot/${it.id}" alt="">
      </div>
    </div>
  `;
}

function confirmDeleteScreenshot(id) {
  const it = (library.screenshots || []).find(s => s.id === id);
  if (!it) return;
  const lightbox = document.getElementById('lightbox');
  if (!lightbox) return;

  // Remove any existing confirmation overlay
  const existing = document.getElementById('delete-confirm');
  if (existing) existing.remove();

  const overlay = document.createElement('div');
  overlay.className = 'delete-confirm';
  overlay.id = 'delete-confirm';
  overlay.innerHTML = `
    <div class="delete-confirm-card">
      <h4>Delete this screenshot?</h4>
      <p>
        <strong>${escHtml(it.filename)}</strong> will be removed from this app's
        local library AND from your Kindle (if it's connected via USB).
        This can't be undone.
      </p>
      <div class="delete-confirm-actions">
        <button class="btn-modal" onclick="cancelDelete()">Cancel</button>
        <button class="btn-modal btn-danger" id="confirm-delete-btn"
                onclick="doDeleteScreenshot('${id}')">Delete</button>
      </div>
      <div class="delete-progress" id="delete-progress" style="display:none"></div>
    </div>
  `;
  lightbox.appendChild(overlay);
}

function cancelDelete() {
  const overlay = document.getElementById('delete-confirm');
  if (overlay) overlay.remove();
}

async function doDeleteScreenshot(id) {
  const btn = document.getElementById('confirm-delete-btn');
  const progress = document.getElementById('delete-progress');
  if (btn) btn.disabled = true;
  if (progress) {
    progress.style.display = 'block';
    progress.textContent = 'Connecting to Kindle…';
  }

  try {
    const r = await fetch(`/api/screenshot/${id}/delete`, { method: 'POST' });
    const d = await r.json();

    if (progress) {
      progress.innerHTML = '';
      for (const line of (d.log || [])) {
        progress.innerHTML += escHtml(line) + '\n';
      }
      if (d.local_deleted && d.device_deleted) {
        progress.innerHTML += '<span style="color:#5a8a5a">✓ Deleted from both places</span>\n';
      } else if (d.local_deleted && !d.device_deleted) {
        progress.innerHTML += '<span style="color:#a07840">⚠ Removed locally; device removal failed (Kindle not connected?)</span>\n';
      }
      progress.scrollTop = progress.scrollHeight;
    }

    // Refresh library and clear the reader pane
    await loadLibrary();
    cancelDelete();
    document.getElementById('reader').innerHTML = `
      <div class="welcome">
        <h2>Screenshot deleted</h2>
        <p>${d.local_deleted ? '✓ Removed from local library' : '✗ Local removal failed'}<br>
           ${d.device_deleted ? '✓ Removed from Kindle' : '⚠ Not removed from Kindle' +
             (d.errors && d.errors.length ? ' — ' + escHtml(d.errors[0]) : '')}</p>
      </div>
    `;
    currentItem = null;
  } catch (e) {
    if (progress) {
      progress.innerHTML += `<span style="color:#a04040">✗ ${escHtml(e.message)}</span>\n`;
    }
    if (btn) btn.disabled = false;
  }
}

function handleKey(e) {
  if (!currentItem || e.target.tagName === 'INPUT') return;
  if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') goPage(-1);
  if (e.key === 'ArrowRight' || e.key === 'ArrowDown') goPage(1);
  if (e.key === '+' || e.key === '=') changeZoom(0.15);
  if (e.key === '-') changeZoom(-0.15);
  if (e.key === '0') resetZoom();
  if (e.key === 'z' || e.key === 'Z') toggleZen();
  if (e.key === 'b' || e.key === 'B') toggleBookMode();
  if (e.key === 'Escape' && document.body.classList.contains('zen-mode')) toggleZen();
}

// ── Zen mode ─────────────────────────────────────────────────────────────────
function toggleZen() {
  if (!currentItem) return;
  const wasZen = document.body.classList.toggle('zen-mode');
  if (wasZen) {
    showZenControlsTemporarily();
    bindZenHover();
  } else {
    unbindZenHover();
  }
  updateChrome();
  // The viewport size changes when entering/leaving zen mode (sidebar +
  // topbar appear/disappear). Re-fit the page dimensions to the new size.
  // setTimeout because the CSS transition needs a frame to settle first.
  setTimeout(() => resizePageContainers(), 50);
}

let _zenHideTimer = null;
let _zenHoverHandler = null;

function showZenControlsTemporarily() {
  document.body.classList.add('show-zen-controls');
  if (_zenHideTimer) clearTimeout(_zenHideTimer);
  _zenHideTimer = setTimeout(() => {
    document.body.classList.remove('show-zen-controls');
  }, 2200);
}

function bindZenHover() {
  if (_zenHoverHandler) return;
  _zenHoverHandler = () => showZenControlsTemporarily();
  document.addEventListener('mousemove', _zenHoverHandler);
}

function unbindZenHover() {
  if (_zenHoverHandler) {
    document.removeEventListener('mousemove', _zenHoverHandler);
    _zenHoverHandler = null;
  }
  document.body.classList.remove('show-zen-controls');
  if (_zenHideTimer) { clearTimeout(_zenHideTimer); _zenHideTimer = null; }
}

// ── Book mode ────────────────────────────────────────────────────────────────
function toggleBookMode() {
  if (!currentItem || totalPages < 3) return;
  bookMode = !bookMode;
  // Persist preference
  try { window.localStorage && window.localStorage.setItem('bookMode', bookMode ? '1' : ''); } catch(e){}
  // Re-render reader from current data without re-fetching
  document.body.classList.toggle('book-mode', bookMode);
  // Update toolbar button state
  const tgl = document.querySelector('.mode-toggle .btn-nav');
  if (tgl) tgl.classList.toggle('active', bookMode);
  // Snap currentPage to a valid spread start in book mode
  if (bookMode) currentPage = snapToSpreadStart(currentPage);
  renderPage();
  updateChrome();
}

// ── Sync ─────────────────────────────────────────────────────────────────────
async function syncAll() {
  const drawer = document.getElementById('sync-drawer');
  const log = document.getElementById('sync-log');
  drawer.style.display = 'block';
  log.innerHTML = '<span class="info">Starting sync…</span>\n';

  try {
    log.innerHTML += '\n<span class="info">▸ Notebooks</span>\n';
    let r = await fetch('/api/sync_notebooks', { method: 'POST' });
    let d = await r.json();
    appendSyncLog(log, d);

    log.innerHTML += '\n<span class="info">▸ Books</span>\n';
    r = await fetch('/api/sync_books', { method: 'POST' });
    d = await r.json();
    appendSyncLog(log, d);

    log.innerHTML += '\n<span class="info">▸ Screenshots</span>\n';
    r = await fetch('/api/sync_screenshots', { method: 'POST' });
    d = await r.json();
    appendSyncLog(log, d);

    await loadLibrary();
    await loadStatus();
    log.innerHTML += '\n<span class="ok">✓ Sync complete</span>\n';
  } catch(e) {
    log.innerHTML += `<span class="err">✗ Request failed: ${escHtml(e.message)}</span>\n`;
  }
}

function appendSyncLog(log, data) {
  for (const line of (data.log || [])) {
    log.innerHTML += escHtml(line) + '\n';
  }
  if (data.ok) {
    log.innerHTML += `<span class="ok">  → ${data.copied} copied, ${data.skipped} up-to-date</span>\n`;
  }
  if (data.errors && data.errors.length) {
    for (const e of data.errors) {
      log.innerHTML += `<span class="err">  ✗ ${escHtml(e)}</span>\n`;
    }
  }
  log.scrollTop = log.scrollHeight;
}

// ── Upload ───────────────────────────────────────────────────────────────────
function showUploadModal() {
  document.getElementById('upload-modal').classList.add('show');
  document.getElementById('upload-status').textContent = '';
  pendingUploadFile = null;
  document.getElementById('btn-upload').disabled = true;
  document.getElementById('drop-zone-text').textContent = 'Drop a file here, or click to browse';
}

function hideUploadModal() {
  document.getElementById('upload-modal').classList.remove('show');
}

function setupDropZone() {
  const dz = document.getElementById('drop-zone');
  const input = document.getElementById('file-input');
  dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('dragover'); });
  dz.addEventListener('dragleave', () => dz.classList.remove('dragover'));
  dz.addEventListener('drop', e => {
    e.preventDefault();
    dz.classList.remove('dragover');
    if (e.dataTransfer.files.length) selectFile(e.dataTransfer.files[0]);
  });
  input.addEventListener('change', e => {
    if (e.target.files.length) selectFile(e.target.files[0]);
  });
}

function selectFile(file) {
  pendingUploadFile = file;
  document.getElementById('drop-zone-text').textContent =
    `${file.name} (${(file.size/1024/1024).toFixed(1)}MB)`;
  document.getElementById('btn-upload').disabled = false;
}

async function doUpload() {
  if (!pendingUploadFile) return;
  const status = document.getElementById('upload-status');
  const btnUpload = document.getElementById('btn-upload');
  btnUpload.disabled = true;
  status.innerHTML = 'Uploading…\n';

  const fd = new FormData();
  fd.append('file', pendingUploadFile);

  try {
    const r = await fetch('/api/upload', { method: 'POST', body: fd });
    const d = await r.json();
    for (const line of (d.log || [])) {
      status.innerHTML += escHtml(line) + '\n';
    }
    if (d.ok) {
      status.innerHTML += `\n<span style="color:#5a8a5a">✓ Pushed to ${escHtml(d.target)} (${d.bytes} bytes)</span>\n`;
      status.innerHTML += '<span style="color:#7a6a5a">Note: file may not appear on home screen until Kindle re-indexes.</span>\n';
    } else {
      for (const e of (d.errors || [])) {
        status.innerHTML += `<span style="color:#a84444">✗ ${escHtml(e)}</span>\n`;
      }
      if (d.error) {
        status.innerHTML += `<span style="color:#a84444">✗ ${escHtml(d.error)}</span>\n`;
      }
    }
  } catch(e) {
    status.innerHTML += `<span style="color:#a84444">✗ ${escHtml(e.message)}</span>\n`;
  } finally {
    btnUpload.disabled = false;
    status.scrollTop = status.scrollHeight;
  }
}

// ── Misc ─────────────────────────────────────────────────────────────────────
async function refreshLibrary() { await loadLibrary(); await loadStatus(); }

async function clearCache() {
  await fetch('/api/clear_cache', { method: 'POST' });
  await loadStatus();
  if (currentItem) openItem(currentItem.id);
}

init();
</script>
</body>
</html>
"""


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Kindle Scribe Library Reader")
    parser.add_argument("--library", default=str(LIBRARY_ROOT),
                        help="Library root directory (default: ~/.scribe_library)")
    parser.add_argument("--port", type=int, default=7070)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--sync", action="store_true",
                        help="Sync notebooks AND books before serving")
    parser.add_argument("--sync-notebooks", action="store_true",
                        help="Only sync notebooks before serving")
    parser.add_argument("--sync-books", action="store_true",
                        help="Only sync books before serving")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    LIBRARY_ROOT = Path(args.library).expanduser()
    PATHS = init_library(LIBRARY_ROOT)

    if args.sync or args.sync_notebooks:
        try:
            from mtp_sync import sync_notebooks
            print("\n⇣ Syncing notebooks…")
            r = sync_notebooks(dest=PATHS.notebooks_dir)
            print(f"   ✓ {r.notebooks_copied} copied, {r.notebooks_skipped} up-to-date")
        except ImportError:
            print("   ✗ mtp_sync.py not found — skipping")

    if args.sync or args.sync_books:
        try:
            from mtp_sync import sync_books
            print("\n⇣ Syncing books…")
            r = sync_books(
                dest=PATHS.books_dir,
                purchased_subdir=PATHS.purchased_dir.name,
                sideloaded_subdir=PATHS.sideloaded_dir.name,
            )
            print(f"   ✓ {r.books_copied} copied, {r.books_skipped} up-to-date")
        except ImportError:
            print("   ✗ mtp_sync.py not found — skipping")

    dep_msg = nbk_to_pdf._check_deps()
    if dep_msg:
        print("\n⚠ Decoder dependencies missing:")
        for line in dep_msg.splitlines():
            print(f"   {line}")

    nb_count = len(library.discover_notebooks(PATHS))
    book_count = len(library.discover_books(PATHS, cache_dir=CACHE_DIR))
    ss_count = len(library.discover_screenshots(PATHS))
    print(f"\n📚 Scribe Library")
    print(f"   Library:  {PATHS.root}")
    print(f"   Found:    {nb_count} notebooks, {book_count} books, {ss_count} screenshots")
    print(f"   Open:     http://{args.host}:{args.port}\n")

    app.run(host=args.host, port=args.port, debug=False)
