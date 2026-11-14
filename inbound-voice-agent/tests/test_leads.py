from inbound_agent.leads import Lead, find_lead
from inbound_agent.phones import check_phone


def test_lead_found_by_caller_id_in_any_format(campaigns_dir):
    path = campaigns_dir.parent / "test_leads.yaml"
    for caller in ("07400123456", "+447400123456", "447400123456"):
        lead = find_lead(path, check_phone(caller, "GB"), "GB")
        assert lead == Lead("Daniel Whitmore", "14 Hawthorn Grove", "Stretford, Manchester",
                            "M32 0JG")


def test_no_lead_for_unknown_or_withheld_number(campaigns_dir):
    path = campaigns_dir.parent / "test_leads.yaml"
    assert find_lead(path, check_phone("07400999999", "GB"), "GB") is None
    assert find_lead(path, check_phone("", "GB"), "GB") is None


def test_no_lookup_without_a_file(tmp_path):
    caller = check_phone("07400123456", "GB")
    assert find_lead("", caller, "GB") is None
    assert find_lead(tmp_path / "missing.yaml", caller, "GB") is None
    broken = tmp_path / "broken.yaml"
    broken.write_text("leads: [", encoding="utf-8")
    assert find_lead(broken, caller, "GB") is None


def test_postcode_normalised(tmp_path):
    path = tmp_path / "leads.yaml"
    path.write_text('leads:\n  - {phone: "07400123456", full_name: A B, first_line: 1 X St, '
                    'postcode: m320jg}\n', encoding="utf-8")
    lead = find_lead(path, check_phone("07400123456", "GB"), "GB")
    assert lead.postcode == "M32 0JG" and lead.has_address
    assert lead.address == "1 X St, M32 0JG"
