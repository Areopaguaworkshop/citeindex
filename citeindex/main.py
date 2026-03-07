import os
import logging
import fitz  # PyMuPDF
from typing import Dict, Optional

from .vertical_llm import VerticalCitationLLM
from .utils import (
    parse_page_range,
    get_input_type,
    determine_url_type,
    save_citation,
    to_csl_json,
    create_subset_pdf,
    extract_pdf_text,
)
from .file_converter import convert_to_pdf
from .type_judge import determine_document_type
from .model import CitationLLM
from .ocr_text_clean_before_llm import clean_extracted_text

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

class CitationExtractor:
    def __init__(self, llm_model="ollama/qwen3"):
        """Initialize the citation extractor."""
        self.llm = CitationLLM(llm_model)

    def extract_citation(
        self,
        input_source: str,
        output_dir: str = "example",
        doc_type_override: Optional[str] = None,
        lang: str = "auto",
        text_direction: str = "horizontal",
        vertical_lang: str = "ch",
        page_range: str = "1-5, -3",
    ) -> Optional[Dict]:
        """Main function to extract citation from any supported source."""
        temp_pdf_path = None
        try:
            input_type = get_input_type(input_source)
            
            # Initialize num_pages to 0
            num_pages = 0

            if input_type == "URL":
                logging.info(f"Detected URL input: {input_source}")
                # This part of the workflow remains unchanged.
                # return self.extract_from_url(input_source, output_dir)
                pass # Placeholder for existing URL logic
            
            elif input_type == "MEDIA_FILE":
                logging.info(f"Detected media file input: {input_source}")
                # This part of the workflow remains unchanged.
                # return self.extract_from_media_file(input_source, output_dir)
                pass # Placeholder for existing media file logic

            # For all document types, we need to count pages first to adjust page_range if needed.
            elif input_type == "OFFICE_DOCUMENT":
                from .file_converter import count_office_document_pages
                logging.info(f"Detected Office document: {input_source}")
                num_pages = count_office_document_pages(input_source)

            elif input_type == "DJVU_DOCUMENT":
                from .file_converter import count_djvu_pages
                logging.info(f"Detected DJVU document: {input_source}")
                num_pages = count_djvu_pages(input_source)

            elif input_type == "PYMUPDF_DOCUMENT":
                logging.info(f"Detected PyMuPDF-supported document: {input_source}")
                num_pages, _ = self._analyze_document_structure(input_source)

            # Adjust page_range based on page count before any conversion or extraction
            if num_pages > 0:
                if num_pages >= 70 and page_range == "1-5, -3":
                    page_range = "1-5"
                    logging.info(f"Large document ({num_pages} pages), adjusting page range to '{page_range}'")
                elif num_pages < 70 and page_range == "1-5, -3":
                    page_range = "1-3, -3"
                    logging.info(f"Small document ({num_pages} pages), adjusting page range to '{page_range}'")

            # Now, proceed with extraction using the (potentially updated) page_range
            if input_type == "OFFICE_DOCUMENT":
                return self.extract_from_office_document_unified(
                    input_source, output_dir, doc_type_override, lang,
                    text_direction, vertical_lang, page_range, num_pages
                )

            elif input_type == "DJVU_DOCUMENT":
                return self.extract_from_djvu_document(
                    input_source, output_dir, doc_type_override, lang,
                    text_direction, vertical_lang, page_range, num_pages
                )

            elif input_type == "PYMUPDF_DOCUMENT":
                if num_pages == 0:
                    logging.error(f"Could not read document file: {input_source}")
                    return None
                return self.extract_from_document(
                    input_source, output_dir, doc_type_override, lang, 
                    text_direction, vertical_lang, page_range, num_pages
                )

            else:
                # This handles URL, MEDIA_FILE, and unknown types
                if input_type not in ["URL", "MEDIA_FILE"]:
                    logging.error(f"Unknown or unsupported input type: {input_source}")
                    if os.path.exists(input_source):
                        logging.error(f"File exists but is not a supported format.")
                    else:
                        logging.error(f"File does not exist: {input_source}")
                return None

        except Exception as e:
            logging.error(f"Error in citation extraction: {e}")
            import traceback
            logging.debug(traceback.format_exc())
            return None
        finally:
            # Ensure temporary PDF from Office conversion is always cleaned up
            if temp_pdf_path and os.path.exists(temp_pdf_path):
                os.remove(temp_pdf_path)
                logging.info(f"Removed temporary file: {temp_pdf_path}")

    def extract_from_document(
        self,
        doc_path: str,
        output_dir: str,
        doc_type_override: Optional[str],
        lang: str,
        text_direction: str,
        vertical_lang: str,
        page_range: str,
        num_pages: int,  # Added num_pages as a required parameter
    ) -> Optional[Dict]:
        """Unified function to extract citation from any document, using a pre-calculated page count."""
        temp_subset_pdf = None
        try:
            print(f"📄 Starting document citation extraction for document with {num_pages} pages...")

            # Step 1: Document Type Pre-filtering by Page Count (using provided num_pages)
            print(f"📊 Step 1: Document type pre-filtering from page count ({num_pages} pages)...")
            if num_pages >= 70:
                allowed_doc_types = ["book", "thesis"]
                page_count_hint = "book"
            else:
                allowed_doc_types = ["journal", "bookchapter"]
                page_count_hint = "journal"

            # Step 2: Create subset PDF from page range, if necessary
            is_temp_file = os.path.basename(doc_path).startswith(('tmp', 'ocr_', 'subset_')) or doc_path.startswith('/tmp/')
            
            if is_temp_file:
                # If the input is already a temporary file, it's the subset we need to process.
                print(f"ℹ️ Step 2: Using pre-converted temporary file: {os.path.basename(doc_path)}")
                temp_subset_pdf = doc_path
                should_cleanup_subset = False # The caller will handle cleanup
            else:
                # If it's the original document, create a subset.
                print(f"✂️ Step 2: Creating temporary PDF from page range '{page_range}'...")
                temp_subset_pdf = create_subset_pdf(doc_path, page_range, num_pages)
                should_cleanup_subset = True # This function should handle cleanup

            if not temp_subset_pdf:
                logging.error("Failed to create or identify subset PDF.")
                return None

            # Step 3: Process the subset PDF to extract text
            print("📝 Step 3: Processing temporary PDF to extract text...")
            accumulated_text = self._process_subset_pdf(temp_subset_pdf, text_direction)

            # Step 3.5: Clean the extracted text before LLM processing
            print("🧹 Step 3.5: Cleaning extracted text...")
            extracted_pages = parse_page_range(page_range, num_pages)
            accumulated_text = clean_extracted_text(
                accumulated_text, num_pages, text_direction, page_range, extracted_pages
            )
            if not accumulated_text.strip():
                print("❌ No text could be extracted from the document.")
                return None

            # Step 4: Document Type Determination
            print("🔍 Step 4: Determining document type...")
            doc_type = doc_type_override or self._determine_document_type_filtered(
                temp_subset_pdf, num_pages, allowed_doc_types, page_count_hint
            )
            print(f"📋 Determined document type: {doc_type.upper()}")

            # Step 5: LLM Extraction
            print(f"🤖 Step 5: Extracting citation from accumulated text with LLM...")
            if text_direction in ["vertical", "auto"] and getattr(self, "_used_vertical_mode", False):
                vertical_llm = VerticalCitationLLM()
                extracted_info = vertical_llm.extract_vertical_citation(accumulated_text, doc_type)
            else:
                extracted_info = self.llm.extract_citation_from_text(accumulated_text, doc_type)
            
            if not extracted_info:
                print("❌ Failed to extract any citation information with LLM.")
                return None

            # Step 6: Convert to CSL JSON and save
            print("💾 Step 6: Converting to CSL JSON and saving...")
            csl_data = to_csl_json(extracted_info, doc_type)
            save_citation(csl_data, output_dir)
            print("✅ Citation extraction completed successfully!")
            return csl_data

        except Exception as e:
            logging.error(f"Error extracting citation from document: {e}")
            import traceback
            logging.debug(traceback.format_exc())
            return None
        finally:
            # Clean up the subset PDF only if this function created it
            if 'should_cleanup_subset' in locals() and should_cleanup_subset and temp_subset_pdf and os.path.exists(temp_subset_pdf):
                os.remove(temp_subset_pdf)
                logging.info(f"Removed temporary subset file: {temp_subset_pdf}")

    def extract_from_office_document_unified(
        self,
        doc_path: str,
        output_dir: str,
        doc_type_override: Optional[str],
        lang: str,
        text_direction: str,
        vertical_lang: str,
        page_range: str,
        num_pages: int,
    ) -> Optional[Dict]:
        """Convert Office document to PDF and proceed with extraction as a PDF document."""
        temp_pdf_path = None
        try:
            from .file_converter import convert_office_to_pdf_range

            if num_pages == 0:
                logging.error(f"Could not read or count pages in Office file: {doc_path}")
                return None

            print("📄 Converting Office document to PDF...")
            # Note: Office conversion to a specific page range is complex.
            # We convert the whole doc then extract pages.
            # The `convert_office_to_pdf_range` handles this.
            temp_pdf_path = convert_office_to_pdf_range(doc_path, page_range, num_pages)
            if not temp_pdf_path:
                logging.error("Failed to convert Office document to PDF.")
                return None

            extracted_data = self.extract_from_document(
                temp_pdf_path, output_dir, doc_type_override, lang, text_direction, vertical_lang, page_range, num_pages
            )
            return extracted_data

        except Exception as e:
            logging.error(f"Error extracting citation from Office document: {e}")
            import traceback
            logging.debug(traceback.format_exc())
            return None
        finally:
            if temp_pdf_path and os.path.exists(temp_pdf_path):
                os.remove(temp_pdf_path)
                logging.info(f"Removed temporary Office document PDF: {temp_pdf_path}")

    def extract_from_djvu_document(
        self,
        doc_path: str,
        output_dir: str,
        doc_type_override: Optional[str],
        lang: str,
        text_direction: str,
        vertical_lang: str,
        page_range: str,
        num_pages: int,
    ) -> Optional[Dict]:
        """Convert DJVU document to PDF and proceed with extraction."""
        temp_pdf_path = None
        try:
            from .file_converter import convert_djvu_to_pdf_range

            if num_pages == 0:
                logging.error(f"Could not read or count pages in DJVU file: {doc_path}")
                return None

            print("📄 Converting DJVU document to PDF...")
            temp_pdf_path = convert_djvu_to_pdf_range(doc_path, page_range, num_pages)
            if not temp_pdf_path:
                logging.error("Failed to convert DJVU document to PDF.")
                return None
            
            # Now that we have a PDF, we can use the standard document extraction flow
            extracted_data = self.extract_from_document(
                temp_pdf_path, output_dir, doc_type_override, lang, text_direction, vertical_lang, page_range, num_pages
            )
            return extracted_data

        except Exception as e:
            logging.error(f"Error extracting citation from DJVU document: {e}")
            import traceback
            logging.debug(traceback.format_exc())
            return None
        finally:
            if temp_pdf_path and os.path.exists(temp_pdf_path):
                os.remove(temp_pdf_path)
                logging.info(f"Removed temporary DJVU document PDF: {temp_pdf_path}")

    def _analyze_document_structure(self, doc_path: str) -> tuple:
        """Analyze document structure using PyMuPDF."""
        try:
            doc = fitz.open(doc_path)
            num_pages = doc.page_count
            doc.close()
            return num_pages, os.path.basename(doc_path)
        except Exception as e:
            # Handle password-protected files gracefully
            if "password" in str(e).lower():
                logging.error(f"The file is password-protected and cannot be processed: {doc_path}")
            else:
                logging.error(f"Error analyzing document structure: {e}")
            return 0, ""

    def _process_subset_pdf(self, pdf_path: str, text_direction: str) -> str:
        """Processes a subset PDF to extract text based on layout."""
        if text_direction == "horizontal":
            return self._process_horizontal_mode(pdf_path)
        elif text_direction == "vertical":
            self._used_vertical_mode = True
            return self._process_vertical_mode(pdf_path)
        elif text_direction == "auto":
            return self._process_auto_mode(pdf_path)
        return ""

    def _process_horizontal_mode(self, pdf_path: str) -> str:
        # (Implementation for horizontal text extraction remains the same)
        from .utils import ensure_searchable_pdf_with_detection
        searchable_pdf_path = ensure_searchable_pdf_with_detection(pdf_path)
        doc = fitz.open(searchable_pdf_path)
        accumulated_text = ""
        for page in doc:
            accumulated_text += page.get_text() + "\n\n"
        doc.close()
        return accumulated_text

    def _process_vertical_mode(self, pdf_path: str) -> str:
        # (Implementation for vertical text extraction remains the same)
        from .vertical_handler import process_vertical_pdf
        return process_vertical_pdf(pdf_path, "ch")

    def _process_auto_mode(self, pdf_path: str) -> str:
        # (Implementation for auto-detection remains the same)
        doc = fitz.open(pdf_path)
        if doc.page_count == 0:
            doc.close()
            return ""
        
        first_page_pix = doc[0].get_pixmap()
        doc.close()
        
        from .vertical_handler import is_vertical_from_layout
        if is_vertical_from_layout(first_page_pix, "ch"):
            self._used_vertical_mode = True
            return self._process_vertical_mode(pdf_path)
        else:
            return self._process_horizontal_mode(pdf_path)

    def _determine_document_type_filtered(self, pdf_path: str, num_pages: int, allowed_types: list, default_type: str) -> str:
        """Determine document type with pre-filtering based on page count."""
        detected_type = determine_document_type(pdf_path, num_pages)
        return detected_type if detected_type in allowed_types else default_type