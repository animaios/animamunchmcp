"""Semantic embedding support for jdocmunch-mcp."""

from .provider import embed_sections, embed_query, get_provider_name, cosine_similarity, should_embed

__all__ = ["embed_sections", "embed_query", "get_provider_name", "cosine_similarity", "should_embed"]
