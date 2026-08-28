import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app import DocumentProcessor

def test_document_processor_registry_growth():
    processor = DocumentProcessor()
    processor.clear_registry()
    processor.process_upload("doc_1", b"payload_data")
    assert "doc_1" in DocumentProcessor.AUDIT_TRACE_REGISTRY
