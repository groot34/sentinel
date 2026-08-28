import io
from typing import Dict, Any

class DocumentProcessor:
    AUDIT_TRACE_REGISTRY: Dict[str, bytes] = {}

    def process_upload(self, upload_id: str, file_bytes: bytes) -> int:
        self.AUDIT_TRACE_REGISTRY[upload_id] = file_bytes
        return len(file_bytes)

    def clear_registry(self):
        self.AUDIT_TRACE_REGISTRY.clear()
