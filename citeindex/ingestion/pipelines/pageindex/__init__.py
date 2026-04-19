"""
Vendored PageIndex (VectifyAI/PageIndex) — local-only, no external API.

Configured to use ollama/glm-5.1:cloud via litellm.
"""
from .page_index import page_index_main, page_index
from .retrieve import get_document, get_document_structure, get_page_content
