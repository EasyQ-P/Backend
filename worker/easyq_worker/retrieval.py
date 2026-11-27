"""Worker-side document processing: text extraction, chunking, keyword tokens, vector checks, overlap (6.1.5 / 6.1.6).
Upload validation (API-21) lives in easyq_backend.documents."""
from io import BytesIO
import math
import unicodedata
import xml.etree.ElementTree as ET
import zipfile

from easyq_common.contracts import APIError





def extract_pages(filename, data):
    """Return one list of text boxes (paragraphs) per page: the input shape of retrieval.chunk_slides
    (AI repo PR #10, slides-v4). PDF: PyMuPDF text blocks in reading order. PPTX: paragraphs per slide."""
    if filename.lower().endswith('.pdf'):
        import pymupdf
        try:
            with pymupdf.open(stream=data, filetype='pdf') as document:
                if document.needs_pass or document.is_encrypted:
                    raise ValueError('Encrypted PDF')
                pages = [[b[4] for b in page.get_text('blocks', sort=True) if b[6] == 0] for page in document]
        except Exception:
            raise APIError(422, 'DOCUMENT_INVALID') from None
    else:
        try:
            with zipfile.ZipFile(BytesIO(data)) as z:
                # Read presentation relationships: physical slide filenames need not match display order.
                rels = ET.fromstring(z.read('ppt/_rels/presentation.xml.rels'))
                targets = {r.attrib['Id']: r.attrib['Target'] for r in rels}
                presentation = ET.fromstring(z.read('ppt/presentation.xml'))
                pages = []
                for node in presentation.iter():
                    if node.tag.endswith('}sldId'):
                        rid = node.attrib['{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id']
                        target = targets[rid]
                        if '..' in target.split('/'):
                            raise ValueError('Invalid relationship')
                        target = target.lstrip('/') if target.startswith('/') else 'ppt/' + target
                        root = ET.fromstring(z.read(target))
                        # One text box per paragraph; runs inside a paragraph are joined without spaces.
                        paragraphs = [''.join(t.text or '' for t in p.iter() if t.tag.endswith('}t')) for p in root.iter() if p.tag.endswith('}p')]
                        pages.append([p for p in paragraphs if p.strip()])
        except (KeyError, ValueError, ET.ParseError, zipfile.BadZipFile):
            raise APIError(422, 'DOCUMENT_INVALID') from None
    if not any(block.strip() for page in pages for block in page):
        raise APIError(422, 'DOCUMENT_NO_TEXT')
    return pages


def page_text(page):
    """Plain text of one page (term extraction and other callers that need a string)."""
    return '\n'.join(page)


def slide_token_spans(tokenizer):
    """bge-reranker-v2-m3 token offsets for chunk_slides. Downloads tokenizer.json on first use unless a local path is set."""
    from retrieval.chunking import bge_token_spans
    return bge_token_spans(tokenizer)


def chunks(pages, token_spans):
    """Slide chunks by the shared retrieval rule (rc-v1 = slides-v4). MIT OCW strip/drop patterns are training-data only."""
    from retrieval.chunking import chunk_slides
    return [{'page_no': c.page_no, 'ordinal': c.part - 1, 'content': c.text} for c in chunk_slides(pages, token_spans)]


def check_vectors(values, count):
    if len(values) != count:
        raise APIError(503, 'EMBEDDING_INVALID')
    for vector in values:
        if len(vector) != 768 or any(type(x) not in (float, int) or not math.isfinite(x) for x in vector) or not any(vector):
            raise APIError(503, 'EMBEDDING_INVALID')
    return values


def keywords(value):
    """Keyword tokens by the shared rule (rc-v1 tokenize_keywords), in order. Search scores them with BM25 over the same
    lecture's READY chunks (6.1.5)."""
    from retrieval.keyword import tokenize_keywords
    return list(tokenize_keywords(value))


def keyword_vector(value):
    """search_vector literal for `%s::tsvector`: every rc-v1 token kept verbatim with its positions.

    to_tsvector('simple', ...) would re-split tokens the shared tokenizer keeps whole (C++ -> c, O(n) -> o, n,
    e-mail -> e-mail, e, mail), so the stored terms, term frequencies and chunk lengths would differ from
    retrieval.keyword.BM25Index. A tsvector literal is stored exactly as written (no parsing or normalisation)."""
    positions = {}
    for n, token in enumerate(keywords(value), 1):
        positions.setdefault(token, []).append(min(n, 16383))   # PostgreSQL position limit
    return ' '.join("'" + token.replace('\\', '\\\\').replace("'", "''") + "':" + ','.join(map(str, places[:256]))
                    for token, places in positions.items())


def vector_tokens(literal):
    """Tokens of a keyword_vector literal back in position order (the SQLite test harness stores the literal as text)."""
    import re
    if "'" not in (literal or ''):
        return (literal or '').split()
    placed = []
    for token, places in re.findall(r"'((?:[^']|'')*)':([0-9,]+)", literal):
        token = token.replace("''", "'").replace('\\\\', '\\')
        placed.extend((int(n), token) for n in places.split(','))
    return [token for _, token in sorted(placed)]


def normalized(value):
    value = unicodedata.normalize('NFKC', value).lower()
    return ' '.join(''.join(c if c.isalnum() else ' ' for c in value).split())


def ngrams(value):
    tokens = normalized(value).split()
    return {' '.join(tokens[i:i + 5]) for i in range(len(tokens) - 4)} if len(tokens) >= 5 else set()


def overlap_ratio(candidate, reference):
    a, b = ngrams(candidate), ngrams(reference)
    return len(a & b) / len(a) if a else float(normalized(candidate) == normalized(reference))
