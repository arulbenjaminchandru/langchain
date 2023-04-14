"""Retriever that combines embedding similarity with recency in retrieving values."""
from copy import deepcopy
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field, validator

from langchain.schema import BaseRetriever, Document
from langchain.vectorstores.faiss import FAISS


def _get_hours_passed(time: datetime, ref_time: datetime) -> float:
    """Get the hours passed between two datetime objects."""
    return (time - ref_time).total_seconds() / 60


class TimeWeightedVectorStoreRetriever(BaseRetriever, BaseModel):
    """Retriever combining embededing similarity with recency."""

    vectorstore: FAISS
    """The vectorstore to store documents and determine salience."""

    search_kwargs: dict = Field(default_factory=lambda: dict(k=100))
    """Keyword arguments to pass to the vectorstore similarity search."""

    # TODO: abstract as a queue
    memory_stream: List[Document] = Field(default_factory=list)
    """The memory_stream of documents to search through."""

    decay_factor: float = Field(default=0.99)
    """The exponential decay factor used as decay_factor ** (hrs_passed)."""

    k: int = 15
    """The maximum number of documents to retrieve in a given call."""

    other_score_keys: List[str] = []
    """Other keys in the metadata to factor into the score, e.g. 'importance'."""

    default_salience: Optional[float] = None
    """The salience to assign memories not retrieved from the vector store.

    None assigns no salience to documents not fetched from the vector store.
    """

    class Config:
        """Configuration for this pydantic object."""

        arbitrary_types_allowed = True

    def _get_combined_score(
        self,
        document: Document,
        vector_salience: Optional[float],
        current_time: datetime,
    ) -> float:
        """Return the combined score for a document."""
        hours_passed = _get_hours_passed(
            current_time,
            document.metadata["last_accessed_at"],
        )
        score = self.decay_factor**hours_passed
        for key in self.other_score_keys:
            if key in document.metadata:
                score += document.metadata[key]
        if vector_salience is not None:
            score += vector_salience
        return score

    @property
    def _similarity_search_with_score(
        self,
    ) -> Callable[[str], List[Tuple[Document, float]]]:
        """Search the vector store for related docs and their similarities."""
        return self.vectorstore.similarity_search_with_score  # type: ignore

    def get_salient_docs(self, query: str) -> Dict[int, Tuple[Document, float]]:
        """Return documents that are salient to the query."""
        docs_and_scores: List[Tuple[Document, float]]
        docs_and_scores = self.vectorstore.similarity_search_with_score(
            query, **self.search_kwargs
        )
        results = {}
        for fetched_doc, cosine_distance in docs_and_scores:
            buffer_idx = fetched_doc.metadata["buffer_idx"]
            doc = self.memory_stream[buffer_idx]
            results[buffer_idx] = (doc, (1 - cosine_distance))
        return results

    def get_relevant_documents(self, query: str) -> List[Document]:
        """Return documents that are relevant to the query."""
        current_time = datetime.now()
        docs_and_scores = {
            doc.metadata["buffer_idx"]: (doc, self.default_salience)
            for doc in self.memory_stream[-self.k :]
        }
        # If a doc is considered salient, update the salience score
        docs_and_scores.update(self.get_salient_docs(query))
        rescored_docs = [
            (doc, self._get_combined_score(doc, salience, current_time))
            for doc, salience in docs_and_scores.values()
        ]
        rescored_docs.sort(key=lambda x: x[1], reverse=True)
        result = []
        # Ensure frequently accessed memories aren't forgotten
        current_time = datetime.now()
        for doc, _ in rescored_docs[: self.k]:
            doc.metadata["last_accessed_at"] = current_time
            result.append(doc)
        return result

    async def aget_relevant_documents(self, query: str) -> List[Document]:
        raise NotImplementedError

    def add_documents(self, documents: List[Document], **kwargs: Any) -> List[str]:
        """Add documents to vectorstore."""
        current_time = kwargs.get("current_time", datetime.now())
        # Avoid mutating input documents
        dup_docs = [deepcopy(d) for d in documents]
        for i, doc in enumerate(dup_docs):
            if "last_accessed_at" not in doc.metadata:
                doc.metadata["last_accessed_at"] = current_time
            if "created_at" not in doc.metadata:
                doc.metadata["created_at"] = current_time
            doc.metadata["buffer_idx"] = len(self.memory_stream) + i
        self.memory_stream.extend(dup_docs)
        return self.vectorstore.add_documents(dup_docs, **kwargs)

    async def aadd_documents(
        self, documents: List[Document], **kwargs: Any
    ) -> List[str]:
        """Add documents to vectorstore."""
        raise NotImplementedError