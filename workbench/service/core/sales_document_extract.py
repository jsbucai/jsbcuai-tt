"""Read-only, source-preserving text extraction for sales attachments.

This module does not identify products or mutate sales drafts. Every result
requires a person to review the original attachment before any order line can
be frozen. PDF text comes from the existing text layer; image OCR is available
only through an explicitly configured local Tesseract executable.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import os
import re
import subprocess


MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
MAX_PAGES = 50
MAX_PAGE_CHARS = 40_000
MAX_TOTAL_CHARS = 200_000
MAX_LINES = 10_000
IMAGE_TYPES = {'image/png', 'image/jpeg', 'image/webp'}
STATUSES = {'needs_review', 'no_text', 'dependency_missing', 'encrypted',
            'invalid', 'limit_exceeded'}


@dataclass(frozen=True)
class DocumentLine:
    page_number: int
    line_number: int
    text: str
    method: str
    source_sha256: str
    source_id: str = ''


@dataclass(frozen=True)
class DocumentPage:
    page_number: int
    status: str
    method: str
    line_count: int


@dataclass(frozen=True)
class DocumentExtraction:
    status: str
    media_type: str
    source_sha256: str
    source_id: str
    page_count: int
    pages: tuple[DocumentPage, ...]
    lines: tuple[DocumentLine, ...]
    unready_pages: tuple[int, ...]
    error_code: str = ''
    requires_human_review: bool = True


@dataclass(frozen=True)
class TesseractConfig:
    command: Path
    languages: tuple[str, ...] = ('chi_sim', 'eng')
    timeout_seconds: int = 15


def _result(status: str, media_type: str, digest: str, source_id: str,
            *, pages=(), lines=(), unready_pages=(), page_count=0,
            error_code='') -> DocumentExtraction:
    if status not in STATUSES:
        raise ValueError('document extraction status is invalid')
    return DocumentExtraction(status, media_type, digest, source_id, page_count,
                              tuple(pages), tuple(lines), tuple(unready_pages),
                              error_code)


def _lines(text: str, page: int, method: str, digest: str, source_id: str):
    return tuple(DocumentLine(page, number, value.strip(), method, digest, source_id)
                 for number, value in enumerate(text.splitlines(), start=1) if value.strip())


def _load_pdf_reader():
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    return PdfReader


def _pdf(raw: bytes, media_type: str, digest: str, source_id: str):
    if not raw.startswith(b'%PDF-'):
        return _result('invalid', media_type, digest, source_id,
                       error_code='pdf_signature_invalid')
    reader_type = _load_pdf_reader()
    if reader_type is None:
        return _result('dependency_missing', media_type, digest, source_id,
                       error_code='pdf_text_extractor_unavailable')
    try:
        reader = reader_type(BytesIO(raw), strict=True)
        if reader.is_encrypted:
            return _result('encrypted', media_type, digest, source_id,
                           error_code='pdf_encrypted')
        page_count = len(reader.pages)
        if not page_count:
            return _result('invalid', media_type, digest, source_id,
                           error_code='pdf_has_no_pages')
        if page_count > MAX_PAGES:
            return _result('limit_exceeded', media_type, digest, source_id,
                           page_count=page_count,error_code='pdf_page_limit')
        pages, lines, unready = [], [], []
        total_chars = 0
        for number, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ''
            if not isinstance(text, str):
                raise ValueError('page text is not a string')
            total_chars += len(text)
            if len(text) > MAX_PAGE_CHARS or total_chars > MAX_TOTAL_CHARS:
                return _result('limit_exceeded', media_type, digest, source_id,
                               page_count=page_count,error_code='pdf_text_limit')
            page_lines = _lines(text, number, 'pdf_text_layer', digest, source_id)
            if len(lines) + len(page_lines) > MAX_LINES:
                return _result('limit_exceeded', media_type, digest, source_id,
                               page_count=page_count,error_code='document_line_limit')
            if page_lines:
                pages.append(DocumentPage(number, 'needs_review', 'pdf_text_layer', len(page_lines)))
                lines.extend(page_lines)
            else:
                pages.append(DocumentPage(number, 'no_text', 'pdf_text_layer', 0))
                unready.append(number)
        return _result('no_text' if unready else 'needs_review', media_type,
                       digest, source_id,pages=pages,lines=lines,
                       unready_pages=unready,page_count=page_count,
                       error_code='pdf_pages_without_text' if unready else '')
    except Exception:
        return _result('invalid', media_type, digest, source_id,
                       error_code='pdf_parse_failed')


def _image_signature(raw: bytes, media_type: str) -> bool:
    return ((media_type == 'image/png' and raw.startswith(b'\x89PNG\r\n\x1a\n')) or
            (media_type == 'image/jpeg' and raw.startswith(b'\xff\xd8\xff')) or
            (media_type == 'image/webp' and raw.startswith(b'RIFF') and
             raw[8:12] == b'WEBP'))


def _tesseract(raw: bytes, media_type: str, digest: str, source_id: str,
               config: TesseractConfig | None):
    if not _image_signature(raw, media_type):
        return _result('invalid', media_type, digest, source_id,
                       page_count=1,error_code='image_signature_invalid')
    if config is None:
        return _result('dependency_missing', media_type, digest, source_id,
                       page_count=1,unready_pages=(1,),error_code='ocr_not_configured')
    command = Path(config.command)
    if (not command.is_absolute() or not command.is_file() or
            not os.access(command, os.X_OK)):
        return _result('dependency_missing', media_type, digest, source_id,
                       page_count=1,unready_pages=(1,),error_code='ocr_executable_unavailable')
    languages = config.languages
    if (not isinstance(languages, tuple) or not languages or
            any(not isinstance(lang, str) or not re.fullmatch(r'[a-z_]{2,20}', lang)
                for lang in languages) or
            not {'chi_sim', 'chi_tra'}.intersection(languages) or
            not isinstance(config.timeout_seconds, int) or
            isinstance(config.timeout_seconds, bool) or
            not 1 <= config.timeout_seconds <= 60):
        return _result('invalid', media_type, digest, source_id,
                       page_count=1,error_code='ocr_config_invalid')
    try:
        listed = subprocess.run([str(command), '--list-langs'],
                                capture_output=True, timeout=config.timeout_seconds,
                                check=False)
    except (OSError, subprocess.TimeoutExpired):
        return _result('dependency_missing', media_type, digest, source_id,
                       page_count=1,unready_pages=(1,),error_code='ocr_languages_unavailable')
    if listed.returncode != 0:
        return _result('dependency_missing', media_type, digest, source_id,
                       page_count=1,unready_pages=(1,),error_code='ocr_languages_unavailable')
    installed = set((listed.stdout + b'\n' + listed.stderr).decode('utf-8', 'replace').splitlines())
    if not set(languages).issubset(installed):
        return _result('dependency_missing', media_type, digest, source_id,
                       page_count=1,unready_pages=(1,),error_code='ocr_chinese_model_missing'
                       if not {'chi_sim','chi_tra'}.intersection(installed) else
                       'ocr_language_model_missing')
    try:
        recognized = subprocess.run([str(command), 'stdin', 'stdout',
                                     '-l', '+'.join(languages)], input=raw,
                                    capture_output=True, timeout=config.timeout_seconds,
                                    check=False)
    except subprocess.TimeoutExpired:
        return _result('no_text', media_type, digest, source_id,
                       page_count=1,unready_pages=(1,),error_code='ocr_timeout')
    except OSError:
        return _result('dependency_missing', media_type, digest, source_id,
                       page_count=1,unready_pages=(1,),error_code='ocr_executable_unavailable')
    if recognized.returncode != 0:
        return _result('no_text', media_type, digest, source_id,
                       page_count=1,unready_pages=(1,),error_code='ocr_failed')
    try:
        content = recognized.stdout.decode('utf-8', 'strict')
    except UnicodeDecodeError:
        return _result('no_text', media_type, digest, source_id,
                       page_count=1,unready_pages=(1,),error_code='ocr_output_invalid')
    if len(content) > MAX_PAGE_CHARS:
        return _result('limit_exceeded', media_type, digest, source_id,
                       page_count=1,error_code='ocr_text_limit')
    lines = _lines(content, 1, 'image_ocr', digest, source_id)
    if not lines:
        return _result('no_text', media_type, digest, source_id,
                       page_count=1,pages=(DocumentPage(1,'no_text','image_ocr',0),),
                       unready_pages=(1,),error_code='ocr_no_text')
    if len(lines) > MAX_LINES:
        return _result('limit_exceeded', media_type, digest, source_id,
                       page_count=1,error_code='document_line_limit')
    return _result('needs_review', media_type, digest, source_id,
                   page_count=1,pages=(DocumentPage(1,'needs_review','image_ocr',len(lines)),),
                   lines=lines)


def extract_document(raw: bytes, media_type: str, *, source_id: str = '',
                     tesseract: TesseractConfig | None = None) -> DocumentExtraction:
    """Extract candidate text with page/line provenance; never infer an order.

    A `needs_review` result only means text was found. It does not mean that
    the document was fully understood or that any SKU, quantity or price is
    suitable for submission.
    """
    if not isinstance(raw, bytes) or not isinstance(media_type, str):
        raise TypeError('document bytes and media type are required')
    if not isinstance(source_id, str) or len(source_id) > 128:
        raise ValueError('source identifier is invalid')
    digest = sha256(raw).hexdigest()
    if not raw:
        return _result('invalid', media_type, digest, source_id,
                       error_code='document_empty')
    if len(raw) > MAX_DOCUMENT_BYTES:
        return _result('limit_exceeded', media_type, digest, source_id,
                       error_code='document_byte_limit')
    if media_type == 'application/pdf':
        return _pdf(raw, media_type, digest, source_id)
    if media_type in IMAGE_TYPES:
        return _tesseract(raw, media_type, digest, source_id, tesseract)
    return _result('invalid', media_type, digest, source_id,
                   error_code='document_type_unsupported')
