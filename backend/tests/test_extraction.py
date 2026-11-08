import pytest

from app.services.extraction import KeywordCodeExtractor
from tests.conftest import FakeRepo
from app.domain.models import MedicalCode


class FullRepo(FakeRepo):
    """Knows every code the extractor can emit."""
    CODES = [MedicalCode(c, "CPT", f"desc {c}", "diagnostic") for c in
             ("77065", "77066", "77067", "76641", "76642", "77048", "77049", "19081", "19083", "19085", "88305",
              "19301", "19303", "38525", "96413", "77385")]


@pytest.fixture
def ex():
    return KeywordCodeExtractor(FullRepo())


def codes(ex, note):
    return {c.code for c in ex.extract(note)}


def test_empty_or_irrelevant_note_gives_nothing(ex):
    assert ex.extract("") == []
    assert ex.extract("Patient has a sore throat and mild fever.") == []


@pytest.mark.parametrize("note,expected", [
    ("Order a screening mammogram.", {"77067"}),
    ("Bilateral diagnostic mammogram requested.", {"77066"}),
    ("Diagnostic mammogram of the left breast.", {"77065"}),
    ("Ultrasound-guided biopsy of the right breast.", {"19083"}),
    ("MRI-guided biopsy recommended.", {"19085"}),
    ("Plan: lumpectomy next month.", {"19301"}),
    ("Start chemotherapy in two weeks.", {"96413"}),
    ("Course of radiation therapy afterwards.", {"77385"}),
    ("Sentinel lymph node biopsy during surgery.", {"38525"}),
])
def test_single_code_rules(ex, note, expected):
    assert expected <= codes(ex, note)


def test_case_insensitive(ex):
    assert "77067" in codes(ex, "SCREENING MAMMOGRAM")


def test_bilateral_suppresses_unilateral(ex):
    got = codes(ex, "bilateral diagnostic mammogram")
    assert "77066" in got and "77065" not in got


def test_bilateral_mri_suppresses_unilateral_mri(ex):
    got = codes(ex, "bilateral breast MRI")
    assert "77049" in got and "77048" not in got


def test_lumpectomy_suppresses_simple_mastectomy(ex):
    got = codes(ex, "partial mastectomy (lumpectomy) planned")
    assert "19301" in got and "19303" not in got


def test_complete_ultrasound_suppresses_limited(ex):
    got = codes(ex, "complete breast ultrasound")
    assert "76641" in got and "76642" not in got


def test_multiple_services_in_one_note(ex):
    got = codes(ex, "Bilateral diagnostic mammogram, then stereotactic biopsy with pathology review.")
    assert {"77066", "19081", "88305"} <= got


def test_each_result_has_evidence_confidence_and_catalog_data(ex):
    r = ex.extract("Needs a screening mammogram.")[0]
    assert "screening mammo" in r.evidence.lower()
    assert 0 < r.confidence <= 1 and r.code_type == "CPT" and r.description == "desc 77067"


def test_codes_missing_from_catalog_are_dropped():
    only = KeywordCodeExtractor(FakeRepo())  # catalog: 77066, 19081, 582
    got = {c.code for c in only.extract("screening mammogram and bilateral diagnostic mammogram")}
    assert got == {"77066"}


def test_no_duplicate_codes(ex):
    out = [c.code for c in ex.extract("lumpectomy lumpectomy lumpectomy")]
    assert len(out) == len(set(out))
