"""Retrieval engine — tokenizer, BM25-Okapi, and ranking signals.

v1.12.0 introduces a real lexical engine to replace the v1.10.0 heuristic
scorer in storage/doc_store.py. The legacy scorer remains accessible via
lexical_engine="legacy" until v2.0.0.
"""
