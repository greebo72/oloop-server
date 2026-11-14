import pytest

from oloop_server.encoding import b64url_decode, b64url_encode


def test_a_round_trip_keeps_every_byte_value():
    data = bytes(range(256))

    assert b64url_decode(b64url_encode(data)) == data


def test_the_encoding_has_no_padding_and_uses_the_url_alphabet():
    assert b64url_encode(b"\xfb\xff") == "-_8"


@pytest.mark.parametrize("text", ["a", "ab=", "ab==", "a b", "a+b/", "ü", "AB\n", "-_9"])
def test_text_that_is_not_the_shortest_form_is_refused(text):
    with pytest.raises(ValueError):
        b64url_decode(text)


def test_the_empty_text_is_empty_bytes():
    assert b64url_decode("") == b""
