# Document Chatbot Demo

A local FastAPI demo for document-grounded answers. It reuses a SQLite-backed retrieval engine that extracts text and page numbers from PDF, Markdown and plain text, splits pages into overlapping chunks, and ranks relevant passages by keywords and document metadata.

## Run locally

Requires Python 3.11 or newer.

```sh
python -m pip install -r requirements.txt
python run.py
```

Open [the chat](http://127.0.0.1:7001/) or [the interactive API docs](http://127.0.0.1:7001/docs). Try “How long is the warranty?” using the fictional lamp manual.

## Demo screen

This local run uses the bundled sample document in excerpt mode. The answer reports a two-year warranty and lists the matching document sections below it.

![Local demo showing a warranty question, the retrieved excerpt, and its document sources](docs/demo-screen.jpg)

## Optional configuration

Copy `test.env` to `.env` and edit the local copy. Real credentials belong in `.env`, which Git ignores.

- Set `DEMO_UPLOAD_TOKEN` to a locally generated secret to enable `POST /api/documents`. Use the `X-Upload-Token` header and attach a Markdown, PDF or text document using the API docs. Uploads are limited to 2 MiB and become searchable.
- Set `GEMINI_API_KEY` and `GEMINI_MODEL` to enable answer generation. This sends the question and matching excerpts to Gemini. Use sample or public documents. Excerpt mode works without a key.
- `data/` stores the local SQLite index and uploaded documents.

## Test

```sh
python -m pytest -q
```
