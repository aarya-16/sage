"""Batch embedder abstraction and implementations."""

import json
import logging
import os
import time
from abc import ABC, abstractmethod
from collections import Counter
from typing import Dict, Generator, List, Optional, Tuple

import marqo
from openai import OpenAI

from sage.chunker import Chunk, Chunker
from sage.data_manager import DataManager

Vector = Tuple[Dict, List[float]]  # (metadata, embedding)


class BatchEmbedder(ABC):
    """Abstract class for batch embedding of a dataset."""

    @abstractmethod
    def embed_dataset(self, chunks_per_batch: int, max_embedding_jobs: int = None):
        """Issues batch embedding jobs for the entire dataset."""

    @abstractmethod
    def embeddings_are_ready(self) -> bool:
        """Checks whether the batch embedding jobs are done."""

    @abstractmethod
    def download_embeddings(self) -> Generator[Vector, None, None]:
        """Yields (chunk_metadata, embedding) pairs for each chunk in the dataset."""


class OpenAIBatchEmbedder(BatchEmbedder):
    """Batch embedder that calls OpenAI. See https://platform.openai.com/docs/guides/batch/overview."""

    def __init__(
        self, data_manager: DataManager, chunker: Chunker, local_dir: str, embedding_model: str, embedding_size: int
    ):
        self.data_manager = data_manager
        self.chunker = chunker
        self.local_dir = local_dir
        self.embedding_model = embedding_model
        self.embedding_size = embedding_size
        self.client = OpenAI()

    def embed_dataset(self, chunks_per_batch: int, max_embedding_jobs: int = None) -> str:
        """Issues batch embedding jobs for the entire dataset. Returns the filename containing the job IDs."""
        batch = []
        batch_ids = {}  # job_id -> metadata
        chunk_count = 0
        dataset_name = self.data_manager.dataset_id.replace("/", "_")

        for content, metadata in self.data_manager.walk():
            chunks = self.chunker.chunk(content, metadata)
            chunk_count += len(chunks)
            batch.extend(chunks)

            if len(batch) > chunks_per_batch:
                for i in range(0, len(batch), chunks_per_batch):
                    sub_batch = batch[i : i + chunks_per_batch]
                    openai_batch_id = self._issue_job_for_chunks(sub_batch, batch_id=f"{dataset_name}/{len(batch_ids)}")
                    batch_ids[openai_batch_id] = [chunk.metadata for chunk in sub_batch]
                    if max_embedding_jobs and len(batch_ids) >= max_embedding_jobs:
                        logging.info("Reached the maximum number of embedding jobs. Stopping.")
                        return
                batch = []

        # Finally, commit the last batch.
        if batch:
            openai_batch_id = self._issue_job_for_chunks(batch, batch_id=f"{dataset_name}/{len(batch_ids)}")
            batch_ids[openai_batch_id] = [chunk.metadata for chunk in batch]
        logging.info("Issued %d jobs for %d chunks.", len(batch_ids), chunk_count)

        timestamp = int(time.time())
        metadata_file = os.path.join(self.local_dir, f"{dataset_name}_openai_batch_ids_{timestamp}.json")
        with open(metadata_file, "w") as f:
            json.dump(batch_ids, f)
        logging.info("Job metadata saved at %s", metadata_file)
        return metadata_file

    def embeddings_are_ready(self, metadata_file: str) -> bool:
        """Checks whether the embeddings jobs are done (either completed or failed).

        Args:
            metadata_file: Path to the file containing the job metadata (output of self.embed_dataset).
        """
        with open(metadata_file, "r") as f:
            batch_ids = json.load(f)

        job_ids = batch_ids.keys()
        statuses = [self.client.batches.retrieve(job_id.strip()) for job_id in job_ids]
        are_ready = all(status.status in ["completed", "failed"] for status in statuses)
        status_counts = Counter(status.status for status in statuses)
        logging.info("Job statuses: %s", status_counts)
        return are_ready

    def download_embeddings(
        self, metadata_file: str, store_file_chunk_content: bool = True
    ) -> Generator[Vector, None, None]:
        """Yields a (chunk_metadata, embedding) pair for each chunk in the dataset.

        Args:
            metadata_file: Path to the file containing the job metadata (output of self.embed_dataset).
            store_file_chunk_content: Whether to store the text content in the metadata for file chunks. Set this to
                False if you want to save space in the vector store. After retrieval, the content of a file chunk can be
                reconstructed based on the file_path, start_byte and end_byte fields in the metadata. This will not
                affect other types of chunks (e.g. GitHub issues) for which the content is harder to reconstruct.
        """
        with open(metadata_file, "r") as f:
            batch_ids = json.load(f)

        job_ids = batch_ids.keys()
        statuses = [self.client.batches.retrieve(job_id.strip()) for job_id in job_ids]

        for idx, status in enumerate(statuses):
            if status.status == "failed":
                logging.error("Job failed: %s", status)
                continue

            if not status.output_file_id:
                error = self.client.files.content(status.error_file_id)
                logging.error("Job %s failed with error: %s", status.id, error.text)
                continue

            batch_metadata = batch_ids[status.id]
            file_response = self.client.files.content(status.output_file_id)
            data = json.loads(file_response.text)["response"]["body"]["data"]
            logging.info("Job %s generated %d embeddings.", status.id, len(data))

            for datum in data:
                idx = int(datum["index"])
                metadata = batch_metadata[idx]
                if (
                    not store_file_chunk_content
                    and "file_path" in metadata
                    and "start_byte" in metadata
                    and "end_byte" in metadata
                ):
                    metadata.pop("text", None)
                embedding = datum["embedding"]
                yield (metadata, embedding)

    def _issue_job_for_chunks(self, chunks: List[Chunk], batch_id: str) -> str:
        """Issues a batch embedding job for the given chunks. Returns the job ID."""
        logging.info("*" * 100)
        logging.info("Issuing job for batch %s with %d chunks.", batch_id, len(chunks))

        # Create a .jsonl file with the batch.
        request = OpenAIBatchEmbedder._chunks_to_request(chunks, batch_id, self.embedding_model, self.embedding_size)
        input_file = os.path.join(self.local_dir, f"batch_{batch_id}.jsonl")
        OpenAIBatchEmbedder._export_to_jsonl([request], input_file)

        # Uplaod the file and issue the embedding job.
        batch_input_file = self.client.files.create(file=open(input_file, "rb"), purpose="batch")
        batch_status = self._create_batch_job(batch_input_file.id)
        logging.info("Created job with ID %s", batch_status.id)
        return batch_status.id

    def _create_batch_job(self, input_file_id: str):
        """Creates a batch embedding job for OpenAI."""
        try:
            return self.client.batches.create(
                input_file_id=input_file_id,
                endpoint="/v1/embeddings",
                completion_window="24h",  # This is the only allowed value for now.
                timeout=3 * 60,  # 3 minutes
                metadata={},
            )
        except Exception as e:
            logging.error(f"Failed to create batch job with input_file_id={input_file_id}. Error: {e}")
            return None

    @staticmethod
    def _export_to_jsonl(list_of_dicts: List[Dict], output_file: str):
        """Exports a list of dictionaries to a .jsonl file."""
        directory = os.path.dirname(output_file)
        if not os.path.exists(directory):
            os.makedirs(directory)
        with open(output_file, "w") as f:
            for item in list_of_dicts:
                json.dump(item, f)
                f.write("\n")

    @staticmethod
    def _chunks_to_request(chunks: List[Chunk], batch_id: str, model: str, dimensions: Optional[int] = None) -> Dict:
        """Convert a list of chunks to a batch request."""
        body = {
            "model": model,
            "input": [chunk.content for chunk in chunks],
        }

        # These are the only two models that support a dynamic embedding size.
        if model in ["text-embedding-3-small", "text-embedding-3-large"] and dimensions is not None:
            body["dimensions"] = dimensions

        return {
            "custom_id": batch_id,
            "method": "POST",
            "url": "/v1/embeddings",
            "body": body,
        }


class MarqoEmbedder(BatchEmbedder):
    """Embedder that uses the open-source Marqo vector search engine.

    Embeddings can be stored locally (in which case `url` the constructor should point to localhost) or in the cloud.
    """

    def __init__(self, data_manager: DataManager, chunker: Chunker, index_name: str, url: str, model="hf/e5-base-v2"):
        self.data_manager = data_manager
        self.chunker = chunker
        self.client = marqo.Client(url=url)
        self.index = self.client.index(index_name)

        all_index_names = [result["indexName"] for result in self.client.get_indexes()["results"]]
        if not index_name in all_index_names:
            self.client.create_index(index_name, model=model)

    def embed_dataset(self, chunks_per_batch: int, max_embedding_jobs: int = None):
        """Issues batch embedding jobs for the entire dataset."""
        if chunks_per_batch > 64:
            raise ValueError("Marqo enforces a limit of 64 chunks per batch.")

        chunk_count = 0
        batch = []
        job_count = 0

        for content, metadata in self.data_manager.walk():
            chunks = self.chunker.chunk(content, metadata)
            chunk_count += len(chunks)
            batch.extend(chunks)

            if len(batch) > chunks_per_batch:
                for i in range(0, len(batch), chunks_per_batch):
                    sub_batch = batch[i : i + chunks_per_batch]
                    logging.info("Indexing %d chunks...", len(sub_batch))
                    self.index.add_documents(
                        documents=[chunk.metadata for chunk in sub_batch],
                        tensor_fields=["text"],
                    )
                    job_count += 1

                    if max_embedding_jobs and job_count >= max_embedding_jobs:
                        logging.info("Reached the maximum number of embedding jobs. Stopping.")
                        return
                batch = []

        # Finally, commit the last batch.
        if batch:
            self.index.add_documents(documents=[chunk.metadata for chunk in batch], tensor_fields=["text"])
        logging.info(f"Successfully embedded {chunk_count} chunks.")

    def embeddings_are_ready(self) -> bool:
        """Checks whether the batch embedding jobs are done."""
        # Marqo indexes documents synchronously, so once embed_dataset() returns, the embeddings are ready.
        return True

    def download_embeddings(self) -> Generator[Vector, None, None]:
        """Yields (chunk_metadata, embedding) pairs for each chunk in the dataset."""
        # Marqo stores embeddings as they are created, so they're already in the vector store. No need to download them
        # as we would with e.g. OpenAI, Cohere, or some other cloud-based embedding service.
        return []


def build_batch_embedder_from_flags(data_manager: DataManager, chunker: Chunker, args) -> BatchEmbedder:
    if args.embedder_type == "openai":
        return OpenAIBatchEmbedder(data_manager, chunker, args.local_dir, args.embedding_model, args.embedding_size)
    elif args.embedder_type == "marqo":
        return MarqoEmbedder(
            data_manager, chunker, index_name=args.index_name, url=args.marqo_url, model=args.embedding_model
        )
    else:
        raise ValueError(f"Unrecognized embedder type {args.embedder_type}")