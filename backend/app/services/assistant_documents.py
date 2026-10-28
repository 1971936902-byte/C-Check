from pathlib import Path
from uuid import UUID

from app.core.config import get_settings


ALLOWED_EXTENSIONS = {'.txt', '.md', '.pdf', '.docx', '.csv', '.json', '.c', '.h'}
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_BATCH_BYTES = 40 * 1024 * 1024
MAX_BATCH_FILES = 20


def document_path(owner_id, document_id) -> Path:
    return get_settings().storage_path / 'assistant' / str(UUID(owner_id)) / (str(UUID(document_id)) + '.bin')


def enqueue_document(document_id):
    from app.tasks.assistant_documents import process_document
    process_document.apply_async(args=[document_id], queue='assistant')
