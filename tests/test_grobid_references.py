from citeindex.ingestion.pipelines.grobid import _parse_tei_references


def test_grobid_preserves_untitled_reference_and_mixed_name_text():
    tei = '''<TEI xmlns="http://www.tei-c.org/ns/1.0"><text><back><listBibl>
      <biblStruct xml:id="r1"><monogr><author><persName><forename>Mary</forename><forename>Ann</forename><surname>Smith</surname></persName></author><imprint><publisher>Press</publisher><pubPlace>Oxford</pubPlace></imprint></monogr></biblStruct>
    </listBibl></back></text></TEI>'''

    reference = _parse_tei_references(tei)[0]

    assert reference["parse_status"] == "untitled"
    assert reference["author"] == [{"family": "Smith", "given": "Mary Ann"}]
    assert reference["publisher-place"] == "Oxford"
    assert reference["tei_id"] == "r1"
