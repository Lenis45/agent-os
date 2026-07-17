import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import import_survey_leads as importer


def test_parse_contact_extracts_telegram_phone_and_email():
    email, phone, telegram = importer.parse_contact("TG @deni_kol, +7 (999) 123-45-67, test@example.com")

    assert email == "test@example.com"
    assert phone == "+79991234567"
    assert telegram == "@deni_kol"


def test_parse_contact_normalizes_russian_8_phone():
    _, phone, telegram = importer.parse_contact("8 916 555-44-33")

    assert phone == "+79165554433"
    assert telegram is None


def test_selected_options_preserves_checkbox_values():
    headers = [
        "ID",
        importer.FEATURE_PREFIX + "Контроль сна",
        importer.FEATURE_PREFIX + "Другое",
    ]
    row = (1, "Да", "свой вариант")

    assert importer.selected_options(headers, row, importer.FEATURE_PREFIX) == [
        "Контроль сна",
        "Другое: свой вариант",
    ]


def test_build_notes_contains_survey_row_and_key_blocks():
    headers = [
        importer.OWNER_COL,
        importer.VET_COL,
        importer.PET_COUNT_COL,
        importer.INTERVIEW_COL,
        importer.CONTACT_COL,
        importer.HEALTH_PREFIX + "Стресс и тревожность",
        importer.FEATURE_PREFIX + "Отслеживание местоположения",
    ]
    row = ("Да", "Нет", "2", "Да", "@lead", "Да", "Да")

    notes = importer.build_notes(headers, row, 7, "@lead")

    assert "[survey_row=7]" in notes
    assert "Количество собак: 2" in notes
    assert "Стресс и тревожность" in notes
    assert "Отслеживание местоположения" in notes
    assert "Raw contact: @lead" in notes


def test_extract_name_from_contact_uses_cyrillic_name():
    name = importer.extract_name_from_contact("Мария Иванова, tg @lead, +7 999 111-22-33")

    assert name == "Мария Иванова"


def test_extract_name_from_contact_rejects_service_words_and_locations():
    assert importer.extract_name_from_contact("Телеграм @lead") is None
    assert importer.extract_name_from_contact(
        "89143458423 - живу во Владивостоке, перед звонком напишите"
    ) is None


def test_lead_display_name_uses_telegram_first_last_handle():
    name, real_name = importer.lead_display_name(
        20,
        "@Ekaterina_Antipina",
        None,
        None,
        "@Ekaterina_Antipina",
    )

    assert name == "Ekaterina Antipina"
    assert real_name == "Ekaterina Antipina"


def test_weeek_description_contains_contact_block_and_notes():
    lead = importer.SurveyLead(
        row_number=12,
        name="Мария Иванова",
        real_name="Мария Иванова",
        email="lead@example.com",
        phone="+79991112233",
        telegram="@lead",
        raw_contact="Мария Иванова @lead",
        pet_count=1,
        notes="[survey_row=12]\nГлавная проблема: тревожность на прогулке",
    )

    description = importer.build_weeek_description(lead)

    assert "КАРТОЧКА ЛИДА AMORI" in description
    assert "КАК СВЯЗАТЬСЯ" in description
    assert "Telegram: @lead" in description
    assert "Телефон: +79991112233" in description
    assert "Email: lead@example.com" in description
    assert "ОТВЕТЫ АНКЕТЫ" in description
    assert "Главная проблема: тревожность на прогулке" in description


def test_weeek_title_prefers_real_name():
    lead = importer.SurveyLead(
        row_number=12,
        name="Мария Иванова",
        real_name="Мария Иванова",
        email=None,
        phone=None,
        telegram="@lead",
        raw_contact="@lead",
        pet_count=1,
        notes="notes",
    )

    assert importer.build_weeek_title(lead) == "Мария Иванова — анкета Amori #12"
