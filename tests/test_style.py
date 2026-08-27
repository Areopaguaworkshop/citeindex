from citeindex.citation_style import format_bibliography

def test_format_bibliography_chicago():
    """Test bibliography formatting with Chicago style."""
    csl_data = [
        {
            "id": "ren-2024-唐朝墩景教寺院圣台和圣堂的考古学研究",
            "type": "article-journal",
            "title": "唐朝墩景教寺院圣台和圣堂的考古学研究",
            "author": [
                {"family": "Ren", "given": "Guan", "literal": "任冠"},
                {"family": "Du", "given": "Meng", "literal": "杜梦"}
            ],
            "issued": {"date-parts": [[2024]]},
            "container-title": "西域研究",
            "volume": "2024",
            "page": "45-55",
            "DOI": "10.16363/j.cnki.xyyj.2024.03.004"
        }
    ]
    
    style = "chicago-author-date"
    
    bibliography, in_text = format_bibliography(csl_data, style)
    
    # Check for the key components, allowing for minor formatting variations
    assert "任冠" in bibliography
    assert "杜梦" in bibliography
    assert "(2024)" in bibliography
    assert "唐朝墩景教寺院圣台和圣堂的考古学研究" in bibliography
    assert "<i>西域研究</i>" in bibliography
    assert "45–55" in bibliography
    assert "10.16363/j.cnki.xyyj.2024.03.004" in bibliography
    
    assert "No in-text citation format defined in this style." in in_text

def test_format_bibliography_not_found_style():
    """Test with a CSL style that does not exist."""
    csl_data = [{"id": "test", "type": "book", "title": "Test Book"}]
    style = "non-existent-style"
    
    bibliography, in_text = format_bibliography(csl_data, style)
    
    assert "Error: Style 'non-existent-style' not found." in bibliography
    assert in_text == ""