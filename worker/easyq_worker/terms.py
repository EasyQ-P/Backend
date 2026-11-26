"""Technical-term extraction for the lecture term dictionary (F2-4, 6.2.3, API-25).

The design fixes the dictionary rules (100 terms, 1~40 chars, host-removed terms never return) but
not the extraction method. The team chose a Kiwi rule-based extractor: no external API, so the
monthly budget cap never blocks it. Candidates are

* compound nouns: 2~4 adjacent noun / English tokens ("해시 함수", "linked list"),
* proper nouns (NNP) and acronyms ("CPU", "RAM"),
* single common nouns or English words that repeat (>= 3 / >= 2 times) in the document.

Terms are ranked by frequency (compounds weigh more) and the first spelling seen is kept.
"""
import re

MAX_TERMS_PER_DOCUMENT = 30
TERM_MAX_LENGTH = 40
NOUN_TAGS = ('NNG', 'NNP')
ACRONYM = re.compile(r'[A-Z][A-Z0-9]{1,5}')
ENGLISH_STOPWORDS = frozenset("""
a an the and or but if then else of to in on at by for from with without into onto over under as is are was were be been
being this that these those it its we you they he she i me my our your their his her them us not no yes do does did done
can could will would shall should may might must have has had having so such than too very via per vs etc e g ie eg also
all any each every some most more less many much few one two three first second next last new old use used using example
examples figure fig table page slide lecture chapter section note notes summary introduction overview question questions
answer answers today week class course part step steps case cases problem problems result results method methods
""".split())


def _is_noun(token):
    return token.tag in NOUN_TAGS or token.tag == 'SL'


def _surface(text, run):
    """Original spelling of a run: slice the page when Kiwi gives positions, else join forms."""
    first, last = run[0], run[-1]
    start, end = getattr(first, 'start', None), getattr(last, 'start', None)
    if start is not None and end is not None and getattr(last, 'len', None) is not None:
        return ' '.join(text[start:end + last.len].split())
    return ' '.join(t.form for t in run)


def _adjacent(text, left, right):
    start, length = getattr(left, 'start', None), getattr(left, 'len', None)
    nxt = getattr(right, 'start', None)
    if start is None or length is None or nxt is None:
        return True
    return 0 <= nxt - (start + length) <= 1 and not text[start + length:nxt].strip()


def _keep(term):
    return 1 <= len(term) <= TERM_MAX_LENGTH and any(c.isalpha() for c in term)


def extract_terms(pages, tokenizer, limit=MAX_TERMS_PER_DOCUMENT):
    """Return up to `limit` candidate terms for one document (list of page texts)."""
    stats = {}  # casefold -> [score, first_seen, spelling, kind, count]
    order = 0

    def add(term, kind, weight):
        nonlocal order
        term = ' '.join(term.split())
        if not _keep(term):
            return
        key = term.casefold()
        if key not in stats:
            stats[key] = [0.0, order, term, kind, 0]
            order += 1
        stats[key][0] += weight
        stats[key][4] += 1

    for page in pages:
        if not page or not page.strip():
            continue
        tokens = list(tokenizer.tokenize(page))
        run = []
        for token in tokens + [None]:
            if token is not None and _is_noun(token) and (not run or _adjacent(page, run[-1], token)):
                run.append(token)
                continue
            if 2 <= len(run) <= 4:
                add(_surface(page, run), 'compound', 1.5 + 0.5 * (len(run) - 2))
            for t in run:
                form = t.form
                if t.tag == 'NNP' and len(form) >= 2:
                    add(form, 'proper', 1.0)
                elif t.tag == 'SL' and ACRONYM.fullmatch(form):
                    add(form, 'acronym', 1.0)
                elif t.tag == 'SL' and len(form) >= 3 and form.lower() not in ENGLISH_STOPWORDS:
                    add(form, 'english', 1.0)
                elif t.tag == 'NNG' and len(form) >= 2:
                    add(form, 'noun', 1.0)
            run = [token] if token is not None and _is_noun(token) else []

    minimum = {'compound': 2, 'proper': 1, 'acronym': 1, 'english': 2, 'noun': 3}
    ranked = sorted((v for v in stats.values() if v[4] >= minimum[v[3]]), key=lambda v: (-v[0], v[1]))
    return [v[2] for v in ranked[:limit]]


def merge_terms(current, removed, extracted, maximum=100):
    """Add extracted terms to the lecture dictionary up to `maximum`, skipping duplicates and
    terms the host removed (case-insensitive). Returns (new_list, added)."""
    blocked = {t.casefold() for t in removed} | {t.casefold() for t in current}
    result, added = list(current), []
    for term in extracted:
        if len(result) >= maximum:
            break
        if term.casefold() in blocked:
            continue
        blocked.add(term.casefold())
        result.append(term)
        added.append(term)
    return result, added
