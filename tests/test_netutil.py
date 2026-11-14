import pytest

from oloop_server.netutil import client_address, parse_networks

PROXY = parse_networks("172.18.0.0/16")


def test_without_a_trusted_proxy_the_header_is_ignored():
    assert client_address("203.0.113.9", "198.51.100.1", []) == "203.0.113.9"


def test_a_header_from_an_untrusted_peer_is_ignored():
    assert client_address("203.0.113.9", "198.51.100.1", PROXY) == "203.0.113.9"


def test_behind_a_trusted_proxy_the_client_is_the_address_it_saw():
    assert client_address("172.18.0.2", "198.51.100.1", PROXY) == "198.51.100.1"


def test_what_the_client_wrote_into_the_header_counts_for_nothing():
    # the client sent "10.0.0.1" itself; the proxy appended the real address
    assert client_address("172.18.0.2", "10.0.0.1, 198.51.100.1", PROXY) == "198.51.100.1"


def test_a_chain_of_trusted_proxies_is_skipped_from_the_right():
    assert client_address("172.18.0.2", "198.51.100.1, 172.18.0.7", PROXY) == "198.51.100.1"


def test_a_chain_that_cannot_be_read_gives_the_peer():
    assert client_address("172.18.0.2", "198.51.100.1, not-an-address", PROXY) == "172.18.0.2"


def test_a_trusted_peer_without_a_header_is_its_own_address():
    assert client_address("172.18.0.2", None, PROXY) == "172.18.0.2"


def test_ipv6_is_normalized():
    assert client_address("172.18.0.2", "2001:DB8:0:0::1", PROXY) == "2001:db8::1"


def test_an_address_of_the_other_family_is_not_in_the_network():
    assert client_address("::1", "198.51.100.1", PROXY) == "::1"


def test_a_peer_that_is_no_address_is_returned_as_it_is():
    assert client_address("testclient", "198.51.100.1", PROXY) == "testclient"


def test_no_peer_at_all():
    assert client_address(None, None, PROXY) == "unknown"


def test_single_addresses_and_networks_can_be_mixed():
    networks = parse_networks(" 10.0.0.5 , 172.18.0.0/16 ,, ")

    assert len(networks) == 2


def test_a_wrong_entry_is_an_error():
    with pytest.raises(ValueError):
        parse_networks("10.0.0.5, proxy.example.org")
