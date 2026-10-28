"""Isolated real OCR acceptance with purely synthetic PNG input."""
from dataclasses import asdict
from hashlib import sha256
import argparse
import json
from pathlib import Path
import subprocess
import time

from core.sales_document_extract import TesseractConfig, extract_document
from core.sales_document_pipeline import _bounded_extract, document_projection


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,required=True)
    parser.add_argument('--command',type=Path,required=True)
    parser.add_argument('--missing-model-command',type=Path,required=True)
    args=parser.parse_args()
    expected=json.loads((args.directory/'expected.json').read_text())
    raw=(args.directory/'synthetic-order.png').read_bytes()
    if sha256(raw).hexdigest()!=expected['image_sha256'] or expected.get('synthetic') is not True:
        raise SystemExit('Synthetic input integrity check failed')
    version=subprocess.run([str(args.command),'--version'],capture_output=True,timeout=10,check=True)
    languages=subprocess.run([str(args.command),'--list-langs'],capture_output=True,timeout=10,check=True)
    config=TesseractConfig(args.command,('chi_sim','eng'),15)
    started=time.monotonic()
    result=_bounded_extract(raw,'image/png','synthetic-order-20261007',config)
    elapsed=time.monotonic()-started
    texts=[line.text for line in result.lines]
    rendered='\n'.join(texts)
    normalized=[''.join(text.split()) for text in texts]
    correct_rows=[value in normalized for value in expected['required_rows']]
    correct_chinese=[value in ''.join(rendered.split()) for value in expected['required_chinese']]
    blank=_bounded_extract((args.directory/'blank-order.png').read_bytes(),'image/png',
                            'synthetic-blank-20261007',config)
    missing=_bounded_extract(raw,'image/png','synthetic-missing-model-20261007',
        TesseractConfig(args.missing_model_command,('chi_sim','eng'),15))
    invalid=extract_document(b'not an image','image/png',source_id='synthetic-invalid-20261007',
                             tesseract=config)
    pdf=_bounded_extract((args.directory/'synthetic-order.pdf').read_bytes(),'application/pdf',
                         'synthetic-pdf-20261007')
    pdf_rows=[line.text for line in pdf.lines]
    evidence={'synthetic_only':True,'no_erp_calls':True,'no_draft_adoption':True,
        'system_install_performed':False,'worker_enabled':False,
        'version':(version.stdout+version.stderr).decode('utf-8','replace'),
        'languages':(languages.stdout+languages.stderr).decode('utf-8','replace'),
        'image_sha256':sha256(raw).hexdigest(),'elapsed_seconds':round(elapsed,3),
        'expected':expected,'required_rows_match':correct_rows,
        'required_chinese_match':correct_chinese,'extraction':asdict(result),
        'projection':document_projection(result),
        'blank_image':asdict(blank),'missing_chinese_model':asdict(missing),
        'invalid_image':asdict(invalid),'text_layer_pdf':asdict(pdf)}
    evidence['synthetic_fixture_passed']=(result.status=='needs_review' and
        all(correct_rows) and all(correct_chinese) and
        blank.status=='no_text' and blank.error_code=='ocr_no_text' and
        missing.status=='dependency_missing' and missing.error_code=='ocr_chinese_model_missing' and
        invalid.status=='invalid' and invalid.error_code=='image_signature_invalid' and
        pdf.status=='needs_review' and pdf.page_count==2 and pdf_rows==expected['required_rows'])
    target=args.directory/'real-ocr-validation.json'
    target.write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n')
    target.chmod(0o600)
    print(json.dumps({'evidence':str(target),'fixture_passed':evidence['synthetic_fixture_passed'],
        'required_rows_match':correct_rows,'required_chinese_match':correct_chinese,
        'elapsed_seconds':evidence['elapsed_seconds'],'extracted_lines':texts,
        'blank':blank.error_code,'missing_model':missing.error_code,'invalid':invalid.error_code},
        ensure_ascii=False))


if __name__=='__main__':
    main()
