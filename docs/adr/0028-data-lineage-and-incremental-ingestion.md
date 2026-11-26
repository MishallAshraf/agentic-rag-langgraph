# 0028. Data lineage and incremental ingestion

- Status: Accepted
- Date: 2026-11-25
- Step: Lampus Phase 1, Step 6

## Context

After Step 5 the same bot held 142 chunks on one machine and 224 in the Codespace, and nothing in the database could say why. Neither copy recorded which files, which file versions, which settings or which code had produced its chunks. For Lampus that matters in two ways:

- **Trust:** when a student or teacher questions an answer, we must be able to show exactly which document, and which version of it, the cited chunk came from.
- **Cost:** every ingest deleted the whole collection and embedded every chunk again. Re-indexing a school's materials after one teacher fixes one typo should not redo the rest.

## Decision

- Record lineage in Postgres (`engine/store/vectors.sql`):
  - `ingest_runs`: one row per ingest, with the collection, folder, start and finish time, code version (git commit, marked `+changes` when the working tree had edits), the settings (embedding model, chunk size and overlap, loader, pipeline version) and what it did.
  - `source_files`: per bot, every file's SHA-256 hash, size, the hash of the settings it was chunked with, and the run that last chunked it.
  - On each chunk: `source_path`, the hash of its text (`content_sha256`), and its `ingest_run`.
- Make ingestion incremental (`engine/lineage.py`, `sync()`), used by `engine/ingest.py` (every bot, and the bot builder) and `engine/step6_ingest.py`:
  - a file whose hash and settings are unchanged is skipped;
  - an edited or new file is re-chunked, and a chunk whose text is unchanged keeps its stored vector;
  - a file removed from the folder has its chunks deleted;
  - changing a setting (or bumping `PIPELINE_VERSION` when the loaders change) re-chunks and re-embeds everything, since old vectors may no longer match.
- Replace each file's chunks in one transaction, and allow one ingest per bot at a time (advisory lock), so a search never sees a half-updated file.
- `python -m engine.lineage chunk <collection> <id>` shows where a chunk came from, and whether its file has been edited since. `python -m engine.lineage runs` lists recent runs.

Why file hashes rather than git: a school's uploaded documents never go into git, so the content hash is the reliable version of a file. The commit records the code version only.

## Measurements

`loadtest/bench_ingest.py`: a copy of the 28 ADRs (224 chunks) ingested three times on a dual-core laptop with a CPU embedding model.

| Run | Before: chunks embedded, time | After: chunks embedded, time |
| --- | --- | --- |
| First ingest | 224, 108 s | 224, 71 s |
| Again, nothing changed | 224, 194 s | 0, 0.3 s |
| One file edited (a paragraph added) | 225, 130 s | 1, 0.4 s |

The large spread in the "before" times is the laptop itself: the same work took 108 s to 194 s.

## Alternatives considered

- **A data versioning tool (DVC, lakeFS):** versions whole datasets and pipelines, which suits training data (Phase 4). For chunks served from a database, rows with hashes and run ids are simpler and queryable with the data.
- **Re-embed whole files only (no chunk reuse):** simpler, but an edit at the end of a 300-page PDF would re-embed all of it.
- **Keep full rebuilds:** simple and always consistent, but cost grows with every school's whole library, and no lineage.

## Consequences

- Re-indexing a bot after small edits takes seconds instead of minutes, and with a paid embedding API it would cost only the changed chunks.
- Every chunk can be traced to a file version, a run, settings and a code version.
- Chunks stored before this step (copied from Chroma) have no lineage; the first ingest of each bot replaces them.
- The Step 1 collection (`documents`, from `engine/step1_ingest.py`) is a learning artefact and still rebuilds in full, without lineage.
- Changes to loaders or splitting must bump `PIPELINE_VERSION`, or old chunks would be kept. Recorded in the code next to the constant.
