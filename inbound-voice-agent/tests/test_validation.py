import pytest

from inbound_agent.validation import is_full_name, normalise_email, spell_out_email


@pytest.mark.parametrize("name", ["Robert Smith", "Mr Robert Smith", "Mary-Jane O'Neil",
                                  "my name is Robert Brown", "it's Priya Patel"])
def test_full_names_accepted(name):
    assert is_full_name(name)


@pytest.mark.parametrize("name", ["Robert", "My name is Robert", "Mr Smith", "Robert J", ""])
def test_first_name_only_rejected(name):
    assert not is_full_name(name)


@pytest.mark.parametrize("spoken,email", [
    ("john dot smith at gmail dot com", "john.smith@gmail.com"),
    ("j smith at g mail dot com", "jsmith@gmail.com"),
    ("robert at bt internet dot com", "robert@btinternet.com"),
    ("Jane_Doe@Outlook.com", "jane_doe@outlook.com"),
])
def test_spoken_emails_normalised(spoken, email):
    assert normalise_email(spoken) == email


@pytest.mark.parametrize("spoken", ["john@gmail", "john at at gmail dot com", "robert", ""])
def test_unclear_emails_refused(spoken):
    assert normalise_email(spoken) == ""


def test_email_read_back():
    assert spell_out_email("j.smith@gmail.com") == "j dot smith at gmail dot com"
