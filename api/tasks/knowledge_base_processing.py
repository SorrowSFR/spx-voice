"""ARQ background task for processing knowledge base documents."""

import os
import tempfile

from loguru import logger

from api.db import db_client
from api.db.models import KnowledgeBaseChunkModel
from api.services.gen_ai import create_embedding_service, resolve_embedding_settings
from api.services.gen_ai.document_processor import process_document_local
from api.services.storage import storage_fs

MAX_FILE_SIZE_BYTES = 25 * 1024 * 1024


def _sanitize_postgres_value(value):
    """Remove NUL bytes because PostgreSQL text/json fields cannot store them."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {
            _sanitize_postgres_value(key): _sanitize_postgres_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_postgres_value(item) for item in value]
    return value


async def process_knowledge_base_document(
    ctx,
    document_id: int,
    s3_key: str,
    organization_id: int,
    created_by_provider_id: str,
    max_tokens: int = 128,
    retrieval_mode: str = "chunked",
):
    """Process a knowledge base document locally: download, parse, embed, store.

    Args:
        ctx: ARQ context
        document_id: Database ID of the document
        s3_key: S3 key where the file is stored
        organization_id: Organization ID
        created_by_provider_id: Uploading user's provider ID (kept for job compatibility)
        max_tokens: Maximum number of tokens per chunk (default: 128)
        retrieval_mode: "chunked" for vector search or "full_document" for full text
    """
    logger.info(
        f"Processing knowledge base document: document_id={document_id}, "
        f"s3_key={s3_key}, org={organization_id}, mode={retrieval_mode}"
    )

    temp_file_path = None

    try:
        await db_client.update_document_status(
            document_id,
            "processing",
            clear_error=True,
        )

        filename = s3_key.split("/")[-1]
        file_extension = os.path.splitext(filename)[1] or ".bin"

        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=file_extension)
        temp_file_path = temp_file.name
        temp_file.close()

        logger.info(f"Downloading file from S3: {s3_key}")
        download_success = await storage_fs.adownload_file(s3_key, temp_file_path)
        if not download_success:
            raise Exception(f"Failed to download file from S3: {s3_key}")
        if not os.path.exists(temp_file_path):
            raise FileNotFoundError(f"Downloaded file not found: {temp_file_path}")

        file_size = os.path.getsize(temp_file_path)
        logger.info(f"Downloaded file size: {file_size} bytes")

        if file_size > MAX_FILE_SIZE_BYTES:
            error_message = (
                f"File size ({file_size / (1024 * 1024):.1f}MB) exceeds the "
                f"maximum allowed size of {MAX_FILE_SIZE_BYTES // (1024 * 1024)}MB."
            )
            logger.warning(f"Document {document_id}: {error_message}")
            await db_client.update_document_status(
                document_id, "failed", error_message=error_message
            )
            return

        file_hash = db_client.compute_file_hash(temp_file_path)
        mime_type = db_client.get_mime_type(temp_file_path)

        document = await db_client.get_document_by_id(document_id)
        if not document:
            raise Exception(f"Document {document_id} not found")

        # Reject duplicates (same hash already ingested for this org).
        existing_doc = await db_client.get_document_by_hash(file_hash, organization_id)
        if existing_doc and existing_doc.id != document_id:
            error_message = (
                f"This file is a duplicate of '{existing_doc.filename}'. "
                f"Please delete the duplicate files and consolidate them into a "
                f"single unique file before uploading."
            )
            logger.warning(
                f"Duplicate document detected: {document_id} is duplicate of "
                f"{existing_doc.id} ({existing_doc.filename})"
            )
            await db_client.update_document_metadata(
                document_id,
                file_size_bytes=file_size,
                file_hash=file_hash,
                mime_type=mime_type,
            )
            await db_client.update_document_status(
                document_id,
                "failed",
                error_message=error_message,
                docling_metadata={
                    "duplicate_of": existing_doc.document_uuid,
                    "duplicate_filename": existing_doc.filename,
                },
            )
            return

        await db_client.update_document_metadata(
            document_id,
            file_size_bytes=file_size,
            file_hash=file_hash,
            mime_type=mime_type,
        )

        logger.info(f"Processing document locally (mode={retrieval_mode})")
        processed = process_document_local(
            file_path=temp_file_path,
            filename=filename,
            mime_type=mime_type or "application/octet-stream",
            retrieval_mode=retrieval_mode,
            max_tokens=max_tokens,
        )

        if retrieval_mode == "full_document":
            await db_client.update_document_full_text(
                document_id,
                _sanitize_postgres_value(processed.full_text),
            )
            await db_client.update_document_status(
                document_id,
                "completed",
                total_chunks=0,
                docling_metadata=_sanitize_postgres_value(processed.metadata),
                clear_error=True,
            )
            logger.info(
                f"Successfully processed full_document {document_id}. "
                f"Text length: {len(processed.full_text)} chars"
            )
            return

        # Chunked mode: fetch embedding config and persist vectorized chunks.
        if document.created_by:
            user_config = await db_client.get_user_configurations(document.created_by)
        else:
            user_config = None
        embedding_settings = resolve_embedding_settings(user_config)
        logger.info(
            f"Using embeddings provider={embedding_settings.get('provider')} "
            f"model={embedding_settings.get('model')}"
        )
        embedding_service = create_embedding_service(
            db_client=db_client,
            provider=embedding_settings.get("provider"),
            api_key=embedding_settings.get("api_key"),
            model=embedding_settings.get("model"),
            base_url=embedding_settings.get("base_url"),
        )

        chunk_records = []
        chunk_texts = []
        for chunk in processed.chunks:
            chunk_text = _sanitize_postgres_value(chunk.chunk_text) or ""
            contextualized_text = (
                _sanitize_postgres_value(chunk.contextualized_text) or chunk_text
            )
            chunk_records.append(
                KnowledgeBaseChunkModel(
                    document_id=document_id,
                    organization_id=organization_id,
                    chunk_text=chunk_text,
                    contextualized_text=contextualized_text,
                    chunk_index=chunk.chunk_index,
                    chunk_metadata=_sanitize_postgres_value(chunk.chunk_metadata),
                    embedding_model=embedding_service.get_model_id(),
                    embedding_dimension=embedding_service.get_embedding_dimension(),
                    token_count=chunk.token_count,
                )
            )
            chunk_texts.append(contextualized_text)

        if not chunk_records:
            logger.warning(
                f"Document {document_id}: local processor returned zero chunks"
            )
            await db_client.update_document_status(
                document_id,
                "completed",
                total_chunks=0,
                docling_metadata=_sanitize_postgres_value(processed.metadata),
                clear_error=True,
            )
            return

        logger.info(
            f"Generating embeddings for {len(chunk_texts)} chunks "
            f"using {embedding_service.get_model_id()}"
        )
        embeddings = await embedding_service.embed_texts(chunk_texts)
        if len(embeddings) != len(chunk_records):
            raise ValueError(
                f"Embedding provider returned {len(embeddings)} embeddings for "
                f"{len(chunk_records)} chunks"
            )
        for chunk_record, embedding in zip(chunk_records, embeddings):
            chunk_record.embedding = embedding

        logger.info("Storing chunks in database")
        await db_client.create_chunks_batch(chunk_records)

        await db_client.update_document_status(
            document_id,
            "completed",
            total_chunks=len(chunk_records),
            docling_metadata=_sanitize_postgres_value(processed.metadata),
            clear_error=True,
        )

        logger.info(
            f"Successfully processed knowledge base document {document_id}. "
            f"Total chunks: {len(chunk_records)}"
        )

    except Exception as e:
        logger.opt(exception=True).error(
            "Error processing knowledge base document {}: {}",
            document_id,
            e,
        )
        await db_client.update_document_status(
            document_id, "failed", error_message=str(e)
        )
        raise

    finally:
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
                logger.debug(f"Cleaned up temp file: {temp_file_path}")
            except Exception as e:
                logger.warning(f"Failed to clean up temp file {temp_file_path}: {e}")
