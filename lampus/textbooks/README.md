# Textbooks

The tutor's textbooks are downloaded, never committed.

```bash
python -m lampus.textbooks.fetch      # the PDF and the web pages of chapters 4-6, into prealgebra-2e/
python -m lampus.ingest_textbook      # chunks with printed page numbers, into the "prealgebra" collection
```

## Prealgebra 2e

- **Publisher:** OpenStax, Rice University. [Book page](https://openstax.org/details/books/prealgebra-2e).
- **License:** Creative Commons Attribution-NonCommercial-ShareAlike 4.0 (CC BY-NC-SA 4.0), as stated on the book page (checked 2026-11-27).
- **What that allows:** using and adapting the book with attribution, for **non-commercial** use, sharing adaptations under the same license. It covers this portfolio project and a free pilot. **It must be replaced before Lampus charges anyone.**
- **Attribution:** "Prealgebra 2e" by Lynn Marecek, MaryAnne Anthony-Smith and Andrea Honeycutt Mathis, OpenStax, licensed CC BY-NC-SA 4.0. Lampus quotes it in tutor replies with page citations, and doesn't change its content.

### Which copy, for what

| | PDF (`prealgebra-2e.pdf`, 63 MB) | Web pages (`web/*.html`, 36 pages, about 15 MB) |
| --- | --- | --- |
| Page numbers students see | yes, in the page footers | no |
| Math (fractions, equations, the answer key) | no: drawn as vector shapes, so extracting the text loses it | yes, as MathML |
| Examples, "Try It" boxes, exercises marked | no | yes |

`lampus/textbook.py` takes the text and math from the web pages and finds each passage's printed page in the PDF. See [ADR 0033](../../docs/adr/0033-textbook-ingestion-printed-pages-and-structure.md).

Chapters fetched: 4 (Fractions), 5 (Decimals), 6 (Percents), with their end-of-chapter pages and answer key.
