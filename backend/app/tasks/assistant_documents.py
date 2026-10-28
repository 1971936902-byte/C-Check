from sqlalchemy import select, update
import httpx
import logging

from app.core.config import get_settings
from app.db.models import AssistantDocument, User
from app.db.session import SessionLocal
from app.services.anythingllm import AnythingLLM
from app.services.assistant_documents import document_path
from app.services.assistant_gate import status
from app.worker import celery_app


def run_document(document_id):
    settings = get_settings()
    if not settings.assistant_enabled:
        return
    with SessionLocal() as db:
        claimed = db.execute(update(AssistantDocument).where(
            AssistantDocument.id == document_id, AssistantDocument.status == 'queued'
        ).values(status='processing', error=None))
        db.commit()
        if claimed.rowcount != 1:
            return
        document = db.get(AssistantDocument, document_id)
        try:
            if settings.deployment_maintenance_file is not None and settings.deployment_maintenance_file.exists():
                document.status = 'paused'
                document.error = 'maintenance'
                db.commit()
                return
            availability = status(db, lease_seconds=settings.assistant_lease_seconds)
            db.rollback()
            if not availability['available']:
                document.status = 'paused'
                document.error = availability['reason']
                db.commit()
                return
            if not settings.assistant_anythingllm_url or not settings.assistant_anythingllm_api_key:
                raise RuntimeError('assistant provider not configured')
            client = AnythingLLM(settings.assistant_anythingllm_url,
                                  settings.assistant_anythingllm_api_key.get_secret_value(), timeout=300)
            db.execute(select(User.id).where(User.id == document.owner_id).with_for_update())
            client.ensure_workspace(document.owner_id, True)
            db.commit()
            if not document.locations:
                with document_path(document.owner_id, document.id).open('rb') as file:
                    document.locations = client.upload_document(document.id, document.filename, file)
                db.commit()
            # Rebuild only this document's vectors when resuming a partial attempt.
            client.remove_documents(document.owner_id, document.locations)
            client.embed_documents(document.owner_id, document.locations)
            document.status = 'ready'
            document.error = None
            db.commit()
        except Exception as exc:
            db.rollback()
            logging.getLogger(__name__).warning('Assistant document processing failed: %s', type(exc).__name__)
            if isinstance(exc, httpx.TransportError) or isinstance(exc.__cause__, httpx.TransportError):
                document.status = 'recovery_required'
                document.error = 'document_upstream_completion_unconfirmed'
            else:
                availability = status(db, lease_seconds=settings.assistant_lease_seconds)
                document.status = 'paused' if not availability['available'] else 'error'
                document.error = availability['reason'] if document.status == 'paused' else 'document_processing_failed'
            db.commit()


@celery_app.task(name='app.tasks.assistant_documents.process_document')
def process_document(document_id):
    run_document(document_id)
