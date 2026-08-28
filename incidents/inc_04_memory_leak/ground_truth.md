# Ground Truth: Incident 04 (Memory Leak from Unclosed File Streaming Buffers)

## Root Cause
Commit `3a9c4e` added `AUDIT_TRACE_REGISTRY` as a class-level dictionary on `DocumentProcessor` and stored full `file_bytes` payloads on every document upload without a TTL, bounded LRU cache, or cleanup mechanism. As documents were uploaded, heap memory grew monotonically until exceeding the 1024MB container cgroup limit, resulting in process termination by Linux OOM killer (exit code 137).

## Causal Chain
1. Code Change: Stored unevicted file byte arrays in class-level dictionary.
2. Runtime Behavior: Garbage collector could not reclaim processed upload payloads.
3. Resource Exhaustion: Container memory reached 995MB / 1024MB limit.
4. Service Crash: Docker daemon killed container with SIGKILL (Exit code 137).

## Distractor
Logs contained warnings about S3 STS temporary token renewal intervals.

## Minimal Fix
Store only metadata (e.g., checksum, byte count, upload ID) in audit traces, or store traces in ephemeral external storage rather than resident process memory.

## Detection Test
`tests/test_service.py::test_document_processor_registry_growth` detects unevicted references in class registry.
