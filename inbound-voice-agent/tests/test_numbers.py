import pytest

from inbound_agent.numbers import convert_numbers_in_stream, number_to_words, replace_numbers

pytest.importorskip("num2words")


def test_cardinal():
    assert number_to_words("499") == "four hundred and ninety-nine"


def test_phone_numbers_read_digit_by_digit():
    assert number_to_words("0412") == "zero four one two"
    assert number_to_words("9557693351").startswith("nine five five")


def test_decimal():
    assert number_to_words("3.5") == "three point five"


def test_devanagari_text_untouched_except_numbers():
    assert "four hundred" in replace_numbers("सिर्फ 499 रुपये")


async def _collect(chunks):
    async def gen():
        for c in chunks:
            yield c
    return "".join([s async for s in convert_numbers_in_stream(gen())])


def _squash(s):
    return " ".join(s.split())


def test_prices_spoken_like_a_person():
    assert _squash(replace_numbers("just £99.99 for 12 months")) == \
        "just ninety-nine pounds ninety-nine for twelve months"
    assert _squash(replace_numbers("£1 or $150 or €0.50")) == \
        "one pound or one hundred and fifty dollars or fifty cents"


async def test_price_split_across_chunks():
    out = _squash(await _collect(["only £", "99", ".", "99 a year."]))
    assert out == "only ninety-nine pounds ninety-nine a year."


async def test_number_split_across_chunks():
    out = await _collect(["It's just 4", "99 a month"])
    assert "four hundred and ninety-nine" in out
    assert "four " not in out.replace("four hundred", "")
