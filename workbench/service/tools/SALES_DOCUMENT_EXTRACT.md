# 销售附件文本提取边界

`core.sales_document_extract.extract_document(raw, media_type, source_id=...)` 是只读、无文件写入的候选文本提取器。它不识别 ERP 商品、不匹配 SKU、不写草稿或订单。调用者必须先按附件表的 SHA-256 校验原始 blob，并在人工核对原件后才能把字段写入草稿。

结果中的 `DocumentLine` 保留 1 起算的 `page_number`、`line_number`、提取方法、附件来源 ID 和原件 SHA-256。`needs_review` 只代表找到了待核对文本；`no_text` 会列出没有文本层的页码，即使同一 PDF 的其他页有文字，也不得标成完整解析。加密、损坏、依赖缺失与容量超限分别返回明确状态和错误码。限制为附件最多 10 MiB、PDF 最多 50 页、每页文本最多 4 万字符、全文最多 20 万字符、最多 1 万条非空行。

结构化 PDF 使用可选的 `pypdf` 文本层，单独记录在 `requirements-documents.txt`，当前生产 `requirements-api.txt` 尚未包含。图片 OCR 只在调用者显式传入本机 `TesseractConfig` 时运行；适配器使用参数列表和 stdin/stdout，不调用 shell，也不读取凭据。运行前查询 `--list-langs`，缺少指定中文模型、命令缺失、超时、解码失败、无文字或图片不能读取均不会返回成功候选。扫描 PDF 目前没有渲染器/OCR 路径，会明确留在 `no_text`。

2026-10-07 只读检查：N150 主机与现运行的 `ul-workbench:0.5.14` 容器均没有 `pdftotext`、`pdfinfo`、`pdftoppm`、`tesseract`、`mutool` 或 Python `pypdf`、`fitz`、`pdfplumber`、`pytesseract`；现有销售和采购隔离 venv 也没有 PDF/OCR Python 包。本模块及依赖尚未部署；现有 App 附件上传仍显示 `needs_processing`，不表示已提取。NAS `generate_photo_quote_match_result.py` 是写死八行的匹配结果样本，并不执行图片 OCR。上线前需独立 worker 的资源隔离与现场中文 OCR 验收。

接口依据：[pypdf 文本提取说明](https://github.com/py-pdf/pypdf/blob/main/docs/user/extract-text.md) 说明扫描件可能没有可提取文本；[Tesseract 命令手册](https://github.com/tesseract-ocr/tesseract/blob/main/doc/tesseract.1.asc) 定义了 stdin/stdout、`--list-langs` 与 `-l` 的用法。
