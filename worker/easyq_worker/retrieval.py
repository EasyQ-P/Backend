"""Worker-side document processing: text extraction, chunking, keyword tokens, vector checks, overlap (6.1.5 / 6.1.6).
Upload validation (API-21) lives in easyq_backend.documents."""
from io import BytesIO
import math
import re
import unicodedata
import xml.etree.ElementTree as ET
import zipfile

from easyq_common.contracts import APIError





def extract_pages(filename, data):
    if filename.lower().endswith('.pdf'):
        from pypdf import PdfReader
        try:
            reader = PdfReader(BytesIO(data), strict=True)
            if reader.is_encrypted:
                raise ValueError('Encrypted PDF')
            pages = [page.extract_text() or '' for page in reader.pages]
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
                        pages.append('\n'.join(' '.join(t.text or '' for t in p.iter() if t.tag.endswith('}t')) for p in root.iter() if p.tag.endswith('}p')))
        except (KeyError, ValueError, ET.ParseError, zipfile.BadZipFile):
            raise APIError(422, 'DOCUMENT_INVALID') from None
    if not any(p.strip() for p in pages):
        raise APIError(422, 'DOCUMENT_NO_TEXT')
    return pages


def chunks(pages, size=800, overlap=100):
    if not 0 <= overlap < size:
        raise ValueError('Invalid chunk configuration')
    result = []
    for page_no, page in enumerate(pages, 1):
        normalized = re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', page)).strip()
        start, index = 0, 0
        while start < len(normalized):
            end = min(start + size, len(normalized))
            if end < len(normalized):
                # Prefer sentence/word boundaries in the final quarter of a chunk.
                candidates = [m.end() for m in re.finditer(r'[.!?。！？](?:\s|$)|\s', normalized[start:end])]
                cut = next((n for n in reversed(candidates) if n >= size * .75), None)
                if cut:
                    end = start + cut
            part = normalized[start:end].strip()
            result.append({'page_no': page_no, 'ordinal': index, 'content': part})
            if end >= len(normalized):
                break
            start = max(start + 1, end - overlap)
            index += 1
    return result


def check_vectors(values, count):
    if len(values) != count:
        raise APIError(503, 'EMBEDDING_INVALID')
    for vector in values:
        if len(vector) != 768 or any(type(x) not in (float, int) or not math.isfinite(x) for x in vector) or not any(vector):
            raise APIError(503, 'EMBEDDING_INVALID')
    return values


def keywords(value, kiwi):
    return ' '.join(t.form.lower() for t in kiwi.tokenize(value) if t.tag.startswith(('N', 'SL', 'SN', 'VV', 'VA')))


def normalized(value):
    value = unicodedata.normalize('NFKC', value).lower()
    return ' '.join(''.join(c if c.isalnum() else ' ' for c in value).split())


def ngrams(value):
    tokens = normalized(value).split()
    return {' '.join(tokens[i:i + 5]) for i in range(len(tokens) - 4)} if len(tokens) >= 5 else set()


def overlap_ratio(candidate, reference):
    a, b = ngrams(candidate), ngrams(reference)
    return len(a & b) / len(a) if a else float(normalized(candidate) == normalized(reference))
