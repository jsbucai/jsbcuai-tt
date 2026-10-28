"""Attachment text remains a review candidate with exact page/line provenance."""
from io import BytesIO
from pathlib import Path
import subprocess
import sys

import pytest

from core import sales_document_extract as extractor
from core.sales_document_extract import TesseractConfig, extract_document


CMAP = b'''/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def
/CMapName /Adobe-Identity-UCS def
/CMapType 2 def
1 begincodespacerange
<00> <FF>
endcodespacerange
2 beginbfchar
<01> <4E2D>
<02> <6587>
endbfchar
endcmap
CMapName currentdict /CMap defineresource pop
end
end'''


def _pdf(*contents, encrypted=False):
    """Build a real text-layer PDF; <0102> extracts as Chinese '中文'."""
    pypdf = pytest.importorskip('pypdf')
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    writer = pypdf.PdfWriter()
    for content in contents:
        page = writer.add_blank_page(width=220, height=220)
        if content is None:
            continue
        stream = DecodedStreamObject()
        stream.set_data(content)
        page[NameObject('/Contents')] = writer._add_object(stream)
        cmap = DecodedStreamObject()
        cmap.set_data(CMAP)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
            NameObject('/Subtype'): NameObject('/Type1'),
            NameObject('/BaseFont'): NameObject('/Helvetica'),
            NameObject('/ToUnicode'): writer._add_object(cmap)})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):
            DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
    if encrypted:
        writer.encrypt('secret')
    result = BytesIO()
    writer.write(result)
    return result.getvalue()


def _fake_tesseract(tmp_path: Path, *, languages='chi_sim\neng',
                    text='客户甲\nLS3073', exit_code=0):
    command = tmp_path / 'fake-tesseract'
    # The interpreter can live below a workspace containing spaces; a shebang
    # cannot quote such a path. Use a local alias for this executable fixture.
    interpreter=tmp_path/'python'
    if not interpreter.exists():interpreter.symlink_to(sys.executable)
    command.write_text(f'''#!{interpreter}
import sys
if '--list-langs' in sys.argv:
    print('List of available languages:')
    print({languages!r})
else:
    sys.stdin.buffer.read()
    print({text!r})
    raise SystemExit({exit_code})
''')
    command.chmod(0o700)
    return command


def test_real_two_page_chinese_pdf_keeps_last_page_and_line_source():
    raw = _pdf(b'BT /F1 12 Tf 10 180 Td <0102> Tj 0 -20 Td (PO-123) Tj ET',
               b'BT /F1 12 Tf 10 180 Td (LS3073) Tj 0 -20 Td (QTY 2) Tj ET')
    result = extract_document(raw, 'application/pdf', source_id='attachment-1')
    assert result.status == 'needs_review' and result.requires_human_review
    assert result.page_count == 2 and not result.unready_pages
    assert [(line.page_number, line.line_number, line.text) for line in result.lines] == [
        (1, 1, '中文'), (1, 2, 'PO-123'), (2, 1, 'LS3073'), (2, 2, 'QTY 2')]
    assert all(line.method == 'pdf_text_layer' and line.source_sha256 == result.source_sha256
               and line.source_id == 'attachment-1' for line in result.lines)
    assert [page.line_count for page in result.pages] == [2, 2]
    assert not hasattr(result, 'erp_order') and not hasattr(result, 'resolved_identity')


def test_blank_last_page_is_explicitly_unready_and_never_claims_complete_extraction():
    result = extract_document(_pdf(b'BT /F1 12 Tf 10 180 Td (PO-123) Tj ET', None),
                              'application/pdf')
    assert result.status == 'no_text' and result.error_code == 'pdf_pages_without_text'
    assert result.page_count == 2 and result.unready_pages == (2,)
    assert [(line.page_number, line.text) for line in result.lines] == [(1, 'PO-123')]
    assert result.pages[1].status == 'no_text'
    scanned = extract_document(_pdf(None, None), 'application/pdf')
    assert scanned.status == 'no_text' and scanned.unready_pages == (1, 2)
    assert scanned.page_count == 2 and not scanned.lines


def test_encrypted_and_corrupt_pdf_never_report_text():
    encrypted = extract_document(_pdf(b'BT /F1 12 Tf 10 180 Td (secret) Tj ET',
                                      encrypted=True), 'application/pdf')
    assert encrypted.status == 'encrypted' and not encrypted.lines
    corrupt = extract_document(b'%PDF-1.7\nnot an object', 'application/pdf')
    assert corrupt.status == 'invalid' and not corrupt.lines


def test_missing_pdf_dependency_reports_not_ready_without_import(monkeypatch):
    monkeypatch.setattr(extractor, '_load_pdf_reader', lambda: None)
    absent = extract_document(b'%PDF-1.7\nreader not installed',
                              'application/pdf')
    assert absent.status == 'dependency_missing'
    assert absent.error_code == 'pdf_text_extractor_unavailable' and not absent.lines


def test_pdf_page_and_text_limits_fail_without_partial_success(monkeypatch):
    raw = _pdf(b'BT /F1 12 Tf 10 180 Td (PO-123) Tj ET',
               b'BT /F1 12 Tf 10 180 Td (LS3073) Tj ET')
    monkeypatch.setattr(extractor, 'MAX_PAGES', 1)
    page_limit = extract_document(raw, 'application/pdf')
    assert page_limit.status == 'limit_exceeded' and not page_limit.lines
    monkeypatch.setattr(extractor, 'MAX_PAGES', 50)
    monkeypatch.setattr(extractor, 'MAX_PAGE_CHARS', 3)
    char_limit = extract_document(raw, 'application/pdf')
    assert char_limit.status == 'limit_exceeded' and not char_limit.lines


def test_image_requires_explicit_ocr_and_chinese_language_model(tmp_path):
    image = b'\x89PNG\r\n\x1a\n' + b'fake-image-bytes'
    missing = extract_document(image, 'image/png')
    assert missing.status == 'dependency_missing' and missing.error_code == 'ocr_not_configured'
    assert missing.unready_pages == (1,) and not missing.lines
    unavailable = extract_document(image, 'image/png',
        tesseract=TesseractConfig(tmp_path / 'not-installed'))
    assert unavailable.status == 'dependency_missing'
    assert unavailable.error_code == 'ocr_executable_unavailable'
    command = _fake_tesseract(tmp_path, languages='eng')
    absent = extract_document(image, 'image/png',
                              tesseract=TesseractConfig(command))
    assert absent.status == 'dependency_missing'
    assert absent.error_code == 'ocr_chinese_model_missing' and not absent.lines


def test_configured_ocr_is_still_a_review_candidate_and_failures_are_not_success(tmp_path):
    image = b'\x89PNG\r\n\x1a\n' + b'fake-image-bytes'
    command = _fake_tesseract(tmp_path)
    result = extract_document(image, 'image/png', source_id='photo-1',
                              tesseract=TesseractConfig(command))
    assert result.status == 'needs_review' and result.requires_human_review
    assert [(line.page_number, line.line_number, line.text, line.method)
            for line in result.lines] == [
                (1, 1, '客户甲', 'image_ocr'), (1, 2, 'LS3073', 'image_ocr')]
    command = _fake_tesseract(tmp_path, exit_code=1)
    failed = extract_document(image, 'image/png',
                              tesseract=TesseractConfig(command))
    assert failed.status == 'no_text' and failed.error_code == 'ocr_failed'
    assert not failed.lines
    invalid = extract_document(b'not-png', 'image/png',
                               tesseract=TesseractConfig(command))
    assert invalid.status == 'invalid' and not invalid.lines


def test_ocr_timeout_never_reports_text_and_subprocess_never_uses_shell(tmp_path, monkeypatch):
    image = b'\x89PNG\r\n\x1a\n' + b'fake-image-bytes'
    command = _fake_tesseract(tmp_path)
    def fake_run(arguments, **kwargs):
        assert isinstance(arguments, list) and 'shell' not in kwargs
        if '--list-langs' in arguments:
            return subprocess.CompletedProcess(arguments, 0, b'chi_sim\neng\n', b'')
        raise subprocess.TimeoutExpired(arguments, kwargs['timeout'])
    monkeypatch.setattr(extractor.subprocess, 'run', fake_run)
    result = extract_document(image, 'image/png',
                              tesseract=TesseractConfig(command, timeout_seconds=1))
    assert result.status == 'no_text' and result.error_code == 'ocr_timeout'
    assert result.unready_pages == (1,) and not result.lines
