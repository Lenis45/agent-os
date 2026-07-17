import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lead_manager import format_lead_list_item


def test_format_lead_list_item_shows_survey_row_and_contact_for_generic_name():
    lead = (
        17,
        "Респондент анкеты #26",
        None,
        None,
        "@lead",
        "survey_2026_07_16",
        "собака",
        "new",
        None,
        "[survey_row=26]\nГлавная проблема: тревожность",
        None,
        None,
        None,
    )

    assert format_lead_list_item(lead) == "#17 Анкета #26 · @lead\n   собака · new · survey_2026_07_16"


def test_format_lead_list_item_keeps_real_name_and_adds_contact():
    lead = (
        20,
        "Павел Шалаев",
        None,
        "+79991234567",
        None,
        "survey_2026_07_16",
        "собака",
        "new",
        None,
        "[survey_row=2]",
        None,
        None,
        None,
    )

    assert format_lead_list_item(lead) == "#20 Павел Шалаев · +79991234567\n   собака · new · survey_2026_07_16"
