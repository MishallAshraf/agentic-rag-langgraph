"""Loading a textbook for the tutor: text and math from the web pages, page numbers from the PDF.

Course topics 18 (document ingestion) and 41 (chunking). Lampus Phase 2, Step 11.

The engine's PDF loader (engine/step6_ingest.py) works for ordinary PDFs, but an
OpenStax math book defeats it twice:

  1. Every formula is drawn as vector shapes, not text. Extracting the text of
     "Simplify: 10/15." gives "Simplify: ." - the fraction is gone, and the
     answer key is almost all numbers, so it comes out nearly empty.
  2. Its page numbers are the PDF's (1, 2, 3...), but the pages students hold
     are numbered from the first chapter: PDF page 285 is printed page 275.
     Citations would send a student 10 pages off.

So each part comes from the copy that has it right:

  - text and math from the book's web pages, where formulas are MathML and
    examples, "Try It" boxes and exercises are marked;
  - printed page numbers from the PDF's page footers ("4.2 • Multiply and
    Divide Fractions     275"), found by searching the PDF for each block's words.

And the book is cut along its own structure instead of every 1,000 characters
(topic 41): one chunk per explanation under a heading, per worked example (with
the "Try It" boxes that follow it), per "How To", per group of exercises. Each
chunk starts with where it is in the book, so it makes sense on its own, and is
tagged with its kind. Answer-key chunks are tagged section "answers", which
search never reads, so the tutor can't find the answers to exercises.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import pymupdf
from bs4 import BeautifulSoup, NavigableString, Tag
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

ROOT = Path(__file__).resolve().parent.parent
BOOK = "Prealgebra 2e"
FOLDER = ROOT / "lampus" / "textbooks" / "prealgebra-2e"
PDF = FOLDER / "prealgebra-2e.pdf"
WEB = FOLDER / "web"

MAX_CHARS = 1800      # a block longer than this is split...
SPLIT_CHARS = 1200    # ...into pieces of about this size
SPLIT_OVERLAP = 150
GROUP_CHARS = 1200    # exercises and answers are grouped up to this size

# ------------------------------------------------------------------ math

OPERATORS = set("+−-×÷=<>≤≥≠⋅·±→≈")


def _wrap(text: str) -> str:
    """Brackets around anything longer than one number or letter: 2/(x+1), not 2/x+1."""
    text = text.strip()
    return text if re.fullmatch(r"[\w.,−-]+", text) else f"({text})"


def math_text(el) -> str:
    """Presentation MathML as plain text a student would type: 10/15, x^2, √(49)."""
    if isinstance(el, NavigableString):
        return str(el).strip()
    name = el.name.split(":")[-1]
    kids = [c for c in el.children if isinstance(c, Tag)]
    if name in ("annotation-xml", "annotation"):
        return ""  # the same formula again, in another form
    if name == "mfrac" and len(kids) == 2:
        # The leading space keeps a mixed number readable: 1 5/6, not 15/6.
        return f" {_wrap(math_text(kids[0]))}/{_wrap(math_text(kids[1]))}"
    if name == "msup" and len(kids) == 2:
        return f"{math_text(kids[0])}^{_wrap(math_text(kids[1]))}"
    if name == "msub" and len(kids) == 2:
        return f"{math_text(kids[0])}_{math_text(kids[1])}"
    if name == "msqrt":
        return f"√({' '.join(math_text(k) for k in kids)})"
    if name == "mroot" and len(kids) == 2:
        return f"root{math_text(kids[1])}({math_text(kids[0])})"
    if name == "mo":
        op = el.get_text().strip()
        return f" {op} " if op in OPERATORS else op
    if name in ("mi", "mn", "mtext", "ms"):
        return el.get_text()
    if name == "mspace":
        return " "
    if name == "mtr":
        return " ".join(math_text(k) for k in kids) + "; "
    return "".join(math_text(k) for k in kids)


def element_text(el: Tag) -> str:
    """An element's text with formulas written out, one line per paragraph or table row."""
    el = BeautifulSoup(str(el), "html.parser")  # a copy: the page itself stays untouched
    for math in el.find_all("math"):
        math.replace_with(NavigableString(" " + re.sub(r"\s+", " ", math_text(math)).strip() + " "))
    for junk in el.find_all(["summary", "img", "script", "style"]):
        junk.decompose()
    for block in el.find_all(["p", "li", "tr", "h3", "h4", "h5", "dt", "dd", "figcaption"]):
        block.append(NavigableString("\n"))
    text = el.get_text(" ")
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r" ?\n[ \n]*", "\n", text)
    return re.sub(r" ([.,;:?)])", r"\1", text).strip()


# ------------------------------------------------------------------ blocks from a web page

@dataclass
class Block:
    kind: str     # explanation, example, how_to, be_prepared, objectives, exercise, key_terms, key_concepts, answer
    title: str    # "Example 4.19", "Exercises: Simplify Fractions"...
    where: str    # the subsection heading it falls under
    text: str


def _classes(el: Tag) -> set[str]:
    return set(el.get("class") or [])


def _title(el: Tag) -> str:
    header = el.find(class_="os-title")
    return re.sub(r"\s+", " ", header.get_text(" ")).strip() if header else ""


def page_title(soup: BeautifulSoup) -> str:
    h = soup.find(attrs={"data-type": "document-title"})
    return re.sub(r"\s+", " ", h.get_text(" ")).strip() if h else ""


def section_blocks(html: str) -> tuple[str, list[Block]]:
    """Cut one section page into blocks, in reading order."""
    soup = BeautifulSoup(html, "html.parser")
    root = soup.find(attrs={"data-book-content": "true"}) or soup
    title = page_title(soup)
    blocks: list[Block] = []
    where = ""

    def add(kind: str, block_title: str, text: str):
        if not text:
            return
        last = blocks[-1] if blocks else None
        if kind == "explanation" and last and last.kind == "explanation" and last.where == where:
            last.text += "\n" + text  # paragraphs under one heading stay together
        elif kind == "exercise" and last and last.kind == "exercise" and last.where == where \
                and len(last.text) + len(text) < GROUP_CHARS:
            last.text += "\n" + text
        else:
            blocks.append(Block(kind, block_title, where, text))

    def visit(el: Tag):
        nonlocal where
        for child in el.children:
            if not isinstance(child, Tag):
                continue
            dtype, cls = child.get("data-type"), _classes(child)
            if dtype == "document-title":
                continue
            if child.name in ("h2", "h3", "h4") and dtype not in ("solution-title",):
                text = element_text(child)
                if text:
                    where = text
                continue
            if dtype == "abstract":
                add("objectives", "Learning Objectives", element_text(child))
            elif dtype == "example":
                add("example", _title(child), element_text(child))
            elif dtype == "note" and "try" in cls:
                # A "Try It" practises the example just before it: keep them in one chunk.
                text = element_text(child)
                if blocks and blocks[-1].kind == "example" and blocks[-1].where == where:
                    blocks[-1].text += "\n" + text
                else:
                    add("try_it", _title(child), text)
            elif dtype == "note" and "howto" in cls:
                add("how_to", "How To", element_text(child))
            elif dtype == "note" and "be-prepared" in cls:
                add("be_prepared", "Be Prepared", element_text(child))
            elif dtype == "note" and cls & {"media-2", "manipulative-math", "media"}:
                continue  # links to videos and activities outside the book
            elif dtype == "exercise":
                add("exercise", f"Exercises: {where}" if where else "Exercises", element_text(child))
            elif dtype == "glossary" or child.name == "dl":
                add("key_terms", "Key Terms", element_text(child))
            elif (child.name in ("section", "div") and not dtype) or dtype == "note" or "os-eos" in cls:
                visit(child)
            else:
                add("explanation", where or title, element_text(child))

    visit(root)
    return title, blocks


def answer_blocks(html: str) -> tuple[str, list[Block]]:
    """The answer key for one chapter: one line per answer, grouped by part and section."""
    soup = BeautifulSoup(html, "html.parser")
    root = soup.find(attrs={"data-book-content": "true"}) or soup
    title = page_title(soup)
    blocks: list[Block] = []
    part = ""
    for el in root.find_all(["h3", "div"]):
        if el.name == "h3":
            part = re.sub(r"\s+", " ", el.get_text(" ")).strip()
            continue
        if el.get("data-type") != "solution":
            continue
        label = el.find("a", class_="os-number")
        number = label.get_text(strip=True) if label else ""
        section = (label.get("data-page-slug") or "") if label else ""
        body = el.find(class_="os-solution-container")
        line = f"{number} {element_text(body) if body else ''}".strip()
        where = f"{part} ({section})" if section else part
        last = blocks[-1] if blocks else None
        if last and last.where == where and len(last.text) + len(line) < GROUP_CHARS:
            last.text += "\n" + line
        else:
            blocks.append(Block("answer", f"Answers: {part}", where, line))
    return title, blocks


# ------------------------------------------------------------------ printed pages from the PDF

FOOTER_LEFT = re.compile(r"^\s*(\d{1,4})\s{2,}\S")    # "254     4 • Fractions"
FOOTER_RIGHT = re.compile(r"\S\s{2,}(\d{1,4})\s*$")   # "4.1 • Visualize Fractions     255"


def words(text: str) -> list[str]:
    """Words of 3+ letters: what the web text and the PDF text have in common (the PDF lost the math)."""
    return re.findall(r"[a-z]{3,}", text.lower())


class PdfPages:
    """The PDF's printed page numbers, and a way to find which page a passage is on."""

    def __init__(self, path: Path = PDF):
        doc = pymupdf.open(path)
        self.toc = doc.get_toc()  # [level, title, 1-based PDF page]
        self.printed: list[int | None] = []
        self.text: list[str] = []  # each page's words, for finding passages
        self.raw: list[str] = []   # each page's text as extracted, for finding answer numbers
        for page in doc:
            raw = page.get_text()
            self.raw.append(raw)
            lines = [line for line in raw.splitlines() if line.strip()]
            number = None
            for line in lines[-3:]:
                m = FOOTER_RIGHT.search(line) or FOOTER_LEFT.search(line)
                if m:
                    number = int(m.group(1))
            self.printed.append(number)
            self.text.append(" ".join(words(raw)))
        # A page without a footer (a chapter's opening photo) takes its neighbours' offset.
        for i, n in enumerate(self.printed):
            if n is None:
                near = next((self.printed[j] - j for j in range(i + 1, min(i + 4, len(self.printed)))
                             if self.printed[j] is not None), None)
                self.printed[i] = i + near if near is not None else i + 1

    def range_of(self, title_prefix: str, level: int = 2, within: str | None = None) -> tuple[int, int]:
        """0-based PDF page range of a table-of-contents entry, up to the next entry at its level or above."""
        entries = self.toc
        if within:  # e.g. "Chapter 4" inside "Answer Key"
            start = next(i for i, e in enumerate(entries) if e[1].startswith(within))
            entries = entries[start:]
        for i, (lvl, title, page) in enumerate(entries):
            if lvl == level and title.startswith(title_prefix):
                end = next((p for lv, _, p in entries[i + 1:] if lv <= level and p > page), page + 1)
                return page - 1, end - 1
        raise KeyError(title_prefix)

    def locate(self, text: str, lo: int, hi: int, n: int = 6) -> int | None:
        """First PDF page in [lo, hi) holding a run of n of the passage's words, or None."""
        w = words(text)
        for offset in range(0, max(1, min(len(w) - n + 1, 60)), 2):
            run = " ".join(w[offset:offset + n])
            if len(run.split()) < min(n, len(w)):
                break
            for p in range(lo, hi):
                if run in self.text[p]:
                    return p
        return None

    def locate_end(self, text: str, start: int, hi: int, n: int = 6) -> int:
        """The PDF page holding the passage's last words (a block can run onto the next page)."""
        w = words(text)
        for offset in range(len(w) - n, max(-1, len(w) - n - 60), -2):
            run = " ".join(w[max(0, offset):offset + n])
            for p in range(start, min(hi, start + 6)):
                if run in self.text[p]:
                    return p
        return start


# ------------------------------------------------------------------ chunks

_splitter = RecursiveCharacterTextSplitter(chunk_size=SPLIT_CHARS, chunk_overlap=SPLIT_OVERLAP)


def page_range(slug: str, pages: PdfPages) -> tuple[int, int]:
    """Where a web page's content sits in the PDF, from the table of contents."""
    m = re.match(r"(\d+)-(\d+)-", slug)
    if m:
        return pages.range_of(f"{m.group(1)}.{m.group(2)} ")
    m = re.match(r"chapter-(\d+)$", slug)
    if m:
        return pages.range_of(f"Chapter {m.group(1)}", within="Answer Key")
    chapter = slug.split("-")[0]  # key terms, key concepts, review exercises, practice test
    lo, _ = pages.range_of("Chapter Review", within=f"Chapter {chapter} ")
    _, hi = pages.range_of(f"Chapter {chapter} ", level=1)
    return lo, hi


def load_page(path: Path, pages: PdfPages) -> tuple[list[Document], dict]:
    """One web page as chunks, each with its printed page. Also returns how many blocks were found in the PDF."""
    slug = path.stem
    html = path.read_text(encoding="utf-8")
    is_answers = slug.startswith("chapter-")
    title, blocks = answer_blocks(html) if is_answers else section_blocks(html)
    lo, hi = page_range(slug, pages)
    chapter = int(re.match(r"(?:chapter-)?(\d+)", slug).group(1))

    docs, cursor, found = [], lo, 0
    for block in blocks:
        pieces = [block.text] if len(block.text) <= MAX_CHARS else _splitter.split_text(block.text)
        for piece in pieces:
            start = pages.locate(piece, cursor, hi)
            if start is None and is_answers:  # answers are mostly numbers: find the first answer's label
                label = re.match(r"\d+\.\d+", piece)
                if label:
                    pattern = re.compile(rf"(?<![\d.]){re.escape(label.group())}(?!\d)")
                    start = next((p for p in range(cursor, hi) if pattern.search(pages.raw[p])), None)
            if start is not None:
                found += 1
                cursor = start  # the book reads forward: the next block is on this page or later
            start = cursor if start is None else start
            end = pages.locate_end(piece, start, hi)
            first, last = pages.printed[start], pages.printed[end]
            context = " › ".join(dict.fromkeys(x for x in (BOOK, title, block.where, block.title) if x))
            docs.append(Document(page_content=f"{context}\n{piece}", metadata={
                "source": BOOK, "page": first, "page_end": last,
                "page_label": str(first) if first == last else f"{first}-{last}",
                "chapter": chapter, "where": f"{title} › {block.where}" if block.where else title,
                "kind": block.kind, "title": block.title,
                "section": "answers" if is_answers else "body",
            }))
    return docs, {"blocks": len(docs), "located": found}
