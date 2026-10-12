"""
RAG sur les fiches techniques et les documents de référence.

Sources indexées :
- les fiches techniques déposées pour les composants et la batterie ;
- les documents du dossier knowledge/ (PDF, Markdown, texte).

Chaque passage récupéré garde son fichier et sa page afin d'être cité
dans le rapport ([S1], [S2]...). Les vecteurs sont mis en cache par
empreinte de fichier : un document n'est vectorisé qu'une seule fois.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import re
from pathlib import Path

from django.conf import settings
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pypdf import PdfReader


logger = logging.getLogger(__name__)

TEXT_SUFFIXES = {'.md', '.txt'}
MAX_PDF_BYTES = 15 * 1024 * 1024

_splitter = RecursiveCharacterTextSplitter(
    chunk_size=900,
    chunk_overlap=150,
)


# ============================================================
# LECTURE DES DOCUMENTS
# ============================================================

def _pages_from_bytes(name, content):
    """Retourne [(page, texte)] pour un PDF ou un fichier texte."""

    if Path(name).suffix.lower() in TEXT_SUFFIXES:
        return [(None, content.decode('utf-8', errors='replace'))]

    if not content.startswith(b'%PDF-') or len(content) > MAX_PDF_BYTES:
        return []

    reader = PdfReader(io.BytesIO(content), strict=False)

    if reader.is_encrypted and not reader.decrypt(''):
        return []

    pages = []

    for number, page in enumerate(reader.pages, start=1):
        text = re.sub(r'\s+', ' ', page.extract_text() or '').strip()
        if text:
            pages.append((number, text))

    return pages


def _project_files(project):
    """Fiches techniques du projet : [(nom affiché, contenu)]."""

    fields = [
        component.datasheet
        for component in project.components.all()
        if component.datasheet
    ]

    battery = getattr(project, 'battery_pref', None)
    if battery is not None and battery.battery_datasheet:
        fields.append(battery.battery_datasheet)

    files = []

    for field in fields:
        try:
            field.open('rb')
            files.append((Path(field.name).name, field.read()))
        except OSError:
            logger.warning('Fiche technique illisible : %s', field.name)
        finally:
            field.close()

    return files


def _knowledge_files():
    directory = Path(settings.RAG_KNOWLEDGE_DIR)

    if not directory.is_dir():
        return []

    return [
        (path.name, path.read_bytes())
        for path in sorted(directory.iterdir())
        if path.suffix.lower() in TEXT_SUFFIXES | {'.pdf'}
        and path.name.lower() != 'readme.md'
    ]


# ============================================================
# INDEX
# ============================================================

def _embeddings():
    from langchain_ollama import OllamaEmbeddings

    return OllamaEmbeddings(
        model=settings.OLLAMA_EMBED_MODEL,
        base_url=settings.OLLAMA_BASE_URL,
    )


def _load_chunks(name, content, embedder):
    """Découpe et vectorise un document, avec cache sur disque."""

    digest = hashlib.sha256(content).hexdigest()
    model_tag = re.sub(r'\W+', '_', settings.OLLAMA_EMBED_MODEL)
    cache_dir = Path(settings.RAG_CACHE_DIR)
    cache_file = cache_dir / f'{digest}_{model_tag}.json'

    if cache_file.exists():
        cached = json.loads(cache_file.read_text(encoding='utf-8'))
        return [Document(page_content=item['text'], metadata={
            'source': name,
            'page': item['page'],
            'vector': item['vector'],
        }) for item in cached]

    documents = [
        Document(page_content=text, metadata={'source': name, 'page': page})
        for page, text in _pages_from_bytes(name, content)
    ]
    chunks = _splitter.split_documents(documents)

    if not chunks:
        return []

    vectors = embedder.embed_documents(
        [chunk.page_content for chunk in chunks]
    ) if embedder else [None] * len(chunks)

    for chunk, vector in zip(chunks, vectors):
        chunk.metadata['vector'] = vector

    if embedder:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps([
            {
                'text': chunk.page_content,
                'page': chunk.metadata['page'],
                'vector': chunk.metadata['vector'],
            }
            for chunk in chunks
        ]), encoding='utf-8')

    return chunks


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def _tokens(text):
    return {
        token for token in re.findall(r'[a-zà-ÿ0-9]{3,}', text.lower())
    }


class DocumentIndex:
    """Index des passages disponibles pour un projet."""

    def __init__(self, project):
        self.chunks = []
        self.semantic = True
        self.error = None
        self.documents = []

        files = _project_files(project) + _knowledge_files()
        self.documents = [name for name, _content in files]

        try:
            embedder = _embeddings()
            for name, content in files:
                self.chunks.extend(_load_chunks(name, content, embedder))

        except Exception as exc:  # Ollama indisponible, modèle absent...
            logger.warning('RAG : vectorisation impossible (%s).', exc)
            self.semantic = False
            self.error = str(exc)
            self.chunks = []
            for name, content in files:
                self.chunks.extend(_load_chunks(name, content, None))

        self._embedder = embedder if self.semantic else None

    def search(self, queries, k=4):
        """Passages les plus pertinents pour une liste de requêtes."""

        if not self.chunks or not queries:
            return []

        scores = {}

        if self.semantic:
            query_vectors = self._embedder.embed_documents(queries)
            for index, chunk in enumerate(self.chunks):
                vector = chunk.metadata.get('vector')
                if vector:
                    scores[index] = max(
                        _cosine(vector, query) for query in query_vectors
                    )
        else:
            # Repli lexical si les embeddings sont indisponibles.
            query_tokens = [_tokens(query) for query in queries]
            for index, chunk in enumerate(self.chunks):
                words = _tokens(chunk.page_content)
                scores[index] = max(
                    len(words & tokens) / (len(tokens) or 1)
                    for tokens in query_tokens
                )

        ranked = sorted(scores, key=scores.get, reverse=True)

        return [
            {
                'source': self.chunks[index].metadata['source'],
                'page': self.chunks[index].metadata['page'],
                'text': self.chunks[index].page_content,
                'score': scores[index],
            }
            for index in ranked[:k]
            if scores[index] > 0
        ]


def number_passages(passages):
    """Attribue les identifiants [S1], [S2]... sans doublon."""

    unique = []
    seen = set()

    for passage in passages:
        key = (passage['source'], passage['page'], passage['text'][:80])
        if key not in seen:
            seen.add(key)
            unique.append(passage)

    for number, passage in enumerate(unique, start=1):
        passage['id'] = f'S{number}'

    return unique
