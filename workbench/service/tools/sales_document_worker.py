"""Extract one queued sales document. Does not adopt, submit, browse or pay."""
from __future__ import annotations

import argparse
from pathlib import Path
import uuid

from core.sales_document_extract import TesseractConfig
from core.sales_document_pipeline import SalesDocumentPipeline
from core.store import Store, dumps


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime',required=True)
    parser.add_argument('--tesseract-command',type=Path)
    parser.add_argument('--languages',default='chi_sim,eng')
    args = parser.parse_args()
    config = None if args.tesseract_command is None else TesseractConfig(
        args.tesseract_command,tuple(args.languages.split(',')))
    pipeline = SalesDocumentPipeline(Store(args.runtime))
    result = pipeline.run_one(uuid.uuid4().hex,tesseract=config)
    # Operational output contains IDs and status only, never attachment text.
    print(dumps({'claimed':result is not None,'job':result}))


if __name__=='__main__':
    main()
