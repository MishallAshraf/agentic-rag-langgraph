"""Lampus Phase 2, Step 11 - Loading a textbook (course topics 18 and 41).

    python -m unittest tests.test_lampus_textbook -v

Small HTML snippets shaped like OpenStax's web pages, and a three-page PDF made
on the fly with footers like the book's, stand in for the real book (which is
downloaded, not committed). No database or model needed.
"""

import sys
import tempfile
import unittest
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup  # noqa: E402

from engine.step1_ingest import format_page  # noqa: E402
from lampus.textbook import PdfPages, answer_blocks, load_page, math_text, section_blocks  # noqa: E402


def mathml(inner: str) -> str:
    return f"<math><semantics><mrow>{inner}</mrow><annotation-xml>DUPLICATE</annotation-xml></semantics></math>"


def math(inner: str) -> str:
    return " ".join(math_text(BeautifulSoup(mathml(inner), "html.parser").find("math")).split())


FRAC_10_15 = "<mfrac><mn>10</mn><mn>15</mn></mfrac>"

SECTION = f"""
<div data-book-content="true">
  <h2 data-type="document-title"><span>4.2</span> <span>Multiply and Divide Fractions</span></h2>
  <div data-type="abstract"><p>By the end of this section, you will be able to simplify fractions.</p></div>
  <section><h3>Simplify Fractions</h3>
    <p>In working with equivalent fractions, you saw that there are many ways to write fractions.</p>
    <p>A fraction is simplified if the numerator and denominator have no common factors.</p>
    <div data-type="note" class="media-2"><p>Watch a video about fractions.</p></div>
    <div data-type="example"><header><h3 class="os-title"><span>Example </span><span>4.19</span></h3></header>
      <p>Simplify: {mathml(FRAC_10_15)}.</p>
      <details data-type="solution"><summary>Show/Hide Solution</summary>
        <h4 data-type="solution-title">Solution</h4><p>Notice that 5 is a factor of both 10 and 15.</p></details>
    </div>
    <div data-type="note" class="try"><header><h3 class="os-title"><span>Try It </span><span>4.37</span></h3></header>
      <p>Simplify: {mathml("<mfrac><mn>8</mn><mn>12</mn></mfrac>")}.</p></div>
  </section>
  <div class="os-eos os-section-exercises-container"><section class="practice-perfect">
    <h4>Simplify Fractions</h4>
    <div data-type="exercise"><p>121. Simplify: {mathml("<mfrac><mn>7</mn><mn>21</mn></mfrac>")}</p></div>
    <div data-type="exercise"><p>122. Simplify: {mathml("<mfrac><mn>8</mn><mn>24</mn></mfrac>")}</p></div>
  </section></div>
</div>"""

ANSWERS = f"""
<div data-book-content="true">
  <h2 data-type="document-title"><span>Chapter 4</span></h2>
  <div class="os-solution-area"><h3 data-type="title">Try It</h3>
    <div data-type="solution"><a class="os-number" data-page-slug="4-2-multiply-and-divide-fractions">4.37</a>
      <div class="os-solution-container"><p>{mathml("<mfrac><mn>2</mn><mn>3</mn></mfrac>")}</p></div></div>
    <div data-type="solution"><a class="os-number" data-page-slug="4-2-multiply-and-divide-fractions">4.38</a>
      <div class="os-solution-container"><p>{mathml("<mfrac><mn>3</mn><mn>4</mn></mfrac>")}</p></div></div>
  </div>
</div>"""


class MathTest(unittest.TestCase):
    def test_fraction(self):
        self.assertEqual(math(FRAC_10_15), "10/15")

    def test_mixed_number_keeps_its_space(self):
        self.assertEqual(math("<mn>1</mn><mfrac><mn>5</mn><mn>6</mn></mfrac>"), "1 5/6")

    def test_brackets_around_longer_parts(self):
        self.assertEqual(math("<mfrac><mrow><mi>x</mi><mo>+</mo><mn>1</mn></mrow><mn>2</mn></mfrac>"), "(x + 1)/2")

    def test_power_and_square_root(self):
        self.assertEqual(math("<msup><mi>x</mi><mn>2</mn></msup>"), "x^2")
        self.assertEqual(math("<msqrt><mn>49</mn></msqrt>"), "√(49)")

    def test_the_duplicate_annotation_is_dropped(self):
        self.assertNotIn("DUPLICATE", math(FRAC_10_15))


class BlocksTest(unittest.TestCase):
    def setUp(self):
        self.title, self.blocks = section_blocks(SECTION)
        self.kinds = [b.kind for b in self.blocks]

    def test_title_and_order(self):
        self.assertEqual(self.title, "4.2 Multiply and Divide Fractions")
        self.assertEqual(self.kinds, ["objectives", "explanation", "example", "exercise"])

    def test_paragraphs_under_one_heading_stay_together(self):
        text = self.blocks[1].text
        self.assertEqual(self.blocks[1].where, "Simplify Fractions")
        self.assertIn("many ways to write fractions", text)
        self.assertIn("no common factors", text)

    def test_example_keeps_its_math_solution_and_try_it(self):
        example = self.blocks[2]
        self.assertEqual(example.title, "Example 4.19")
        self.assertIn("Simplify: 10/15.", example.text)
        self.assertIn("Notice that 5 is a factor", example.text)
        self.assertIn("8/12", example.text)  # the Try It that practises it
        self.assertNotIn("Show/Hide", example.text)

    def test_videos_are_skipped(self):
        self.assertFalse(any("Watch a video" in b.text for b in self.blocks))

    def test_exercises_are_grouped(self):
        exercises = self.blocks[3]
        self.assertEqual(exercises.title, "Exercises: Simplify Fractions")
        self.assertIn("7/21", exercises.text)
        self.assertIn("8/24", exercises.text)

    def test_answers(self):
        title, blocks = answer_blocks(ANSWERS)
        self.assertEqual(title, "Chapter 4")
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].kind, "answer")
        self.assertEqual(blocks[0].text, "4.37 2/3\n4.38 3/4")


def make_pdf(path: Path):
    """Three pages numbered like the book: PDF pages 1-2 are printed 274-275, page 3 is in the answer key."""
    doc = pymupdf.open()
    pages = [
        ["In working with equivalent fractions, you saw that there are many ways to write fractions.",
         "A fraction is simplified if the numerator and denominator have no common factors.",
         "274     4 • Fractions"],
        ["EXAMPLE 4.19", "Simplify:", "Solution", "Notice that 5 is a factor of both 10 and 15.",
         "121. Simplify:", "4.2 • Multiply and Divide Fractions     275"],
        ["Try It", "4.37", "4.38", "Answer Key     996"],
    ]
    for lines in pages:
        page = doc.new_page()
        for i, line in enumerate(lines):
            page.insert_text((72, 72 + 20 * i), line, fontsize=10)
    doc.set_toc([[1, "Chapter 4 Fractions", 1], [2, "4.2 Multiply and Divide Fractions", 1],
                 [1, "Answer Key", 3], [2, "Chapter 4", 3], [2, "Chapter 5", 4]])
    doc.save(path)


class PdfPagesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        make_pdf(cls.dir / "book.pdf")
        cls.pages = PdfPages(cls.dir / "book.pdf")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_printed_page_numbers_come_from_the_footers(self):
        self.assertEqual(self.pages.printed, [274, 275, 996])

    def test_ranges_from_the_table_of_contents(self):
        self.assertEqual(self.pages.range_of("4.2 "), (0, 2))
        self.assertEqual(self.pages.range_of("Chapter 4", within="Answer Key"), (2, 3))

    def test_a_passage_is_found_by_its_words(self):
        self.assertEqual(self.pages.locate("Notice that 5 is a factor of both 10 and 15.", 0, 3), 1)
        self.assertIsNone(self.pages.locate("words that are nowhere in this little book at all", 0, 3))

    def test_chunks_cite_printed_pages_and_answers_are_set_apart(self):
        web = self.dir / "web"
        web.mkdir(exist_ok=True)
        (web / "4-2-multiply-and-divide-fractions.html").write_text(SECTION, encoding="utf-8")
        (web / "chapter-4.html").write_text(ANSWERS, encoding="utf-8")

        docs, stats = load_page(web / "4-2-multiply-and-divide-fractions.html", self.pages)
        by_kind = {d.metadata["kind"]: d for d in docs}
        self.assertEqual(by_kind["explanation"].metadata["page_label"], "274")
        self.assertEqual(by_kind["example"].metadata["page_label"], "275")
        self.assertTrue(by_kind["example"].page_content.startswith(
            "Prealgebra 2e › 4.2 Multiply and Divide Fractions › Simplify Fractions › Example 4.19\n"))
        self.assertTrue(all(d.metadata["section"] == "body" for d in docs))
        self.assertEqual(format_page(by_kind["example"].metadata), "275")

        answers, _ = load_page(web / "chapter-4.html", self.pages)
        self.assertEqual([d.metadata["section"] for d in answers], ["answers"])
        self.assertEqual(answers[0].metadata["page"], 996)


class FormatPageTest(unittest.TestCase):
    def test_printed_label_wins_over_the_pdf_page(self):
        self.assertEqual(format_page({"page": 284, "page_label": "275"}), "275")
        self.assertEqual(format_page({"page": 3}), "4")
        self.assertEqual(format_page({}), "-")


if __name__ == "__main__":
    unittest.main()
