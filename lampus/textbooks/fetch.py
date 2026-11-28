"""Download the tutor's textbook: the PDF and the web pages of the chapters used.

    python -m lampus.textbooks.fetch

OpenStax Prealgebra 2e (CC BY-NC-SA 4.0, see README.md). Two copies of the
same book, because each has what the other lacks:

  - the PDF has the printed page numbers students see, but draws every
    formula as vector shapes, so text extraction loses all the math;
  - the web pages have the math as text (MathML) and mark examples, "Try It"
    boxes and exercises, but have no page numbers.

Files go to lampus/textbooks/prealgebra-2e/ (not committed). Pages are fetched
one at a time with a pause, and skipped if already there.
"""

import re
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent / "prealgebra-2e"
PDF_URL = "https://assets.openstax.org/oscms-prodcms/media/documents/prealgebra-2e_-_WEB.pdf"
WEB = "https://openstax.org/books/prealgebra-2e/pages/"
CHAPTERS = (4, 5, 6)
# Section titles from the PDF's table of contents; the web page names are made from them.
SECTIONS = {
    4: ["Visualize Fractions", "Multiply and Divide Fractions",
        "Multiply and Divide Mixed Numbers and Complex Fractions",
        "Add and Subtract Fractions with Common Denominators",
        "Add and Subtract Fractions with Different Denominators",
        "Add and Subtract Mixed Numbers", "Solve Equations with Fractions"],
    5: ["Decimals", "Decimal Operations", "Decimals and Fractions", "Solve Equations with Decimals",
        "Averages and Probability", "Ratios and Rate", "Simplify and Use Square Roots"],
    6: ["Understand Percent", "Solve General Applications of Percent",
        "Solve Sales Tax, Commission, and Discount Applications", "Solve Simple Interest Applications",
        "Solve Proportions and their Applications"],
}
END_OF_CHAPTER = ["key-terms", "key-concepts", "review-exercises", "practice-test"]


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def pages() -> list[str]:
    names = []
    for ch in CHAPTERS:
        names += [f"{ch}-{n}-{slug(title)}" for n, title in enumerate(SECTIONS[ch], start=1)]
        names += [f"{ch}-{part}" for part in END_OF_CHAPTER]
        names.append(f"chapter-{ch}")  # the chapter's part of the answer key
    return names


def get(url: str, dest: Path) -> str:
    if dest.exists():
        return "kept"
    request = urllib.request.Request(url, headers={"User-Agent": "lampus-textbook-fetch/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            dest.write_bytes(response.read())
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}"
    time.sleep(1)  # be polite to the publisher's servers
    return f"{dest.stat().st_size // 1024} KB"


def main():
    (HERE / "web").mkdir(parents=True, exist_ok=True)
    print("prealgebra-2e.pdf", get(PDF_URL, HERE / "prealgebra-2e.pdf"))
    for name in pages():
        print(name, get(WEB + name, HERE / "web" / f"{name}.html"))


if __name__ == "__main__":
    main()
