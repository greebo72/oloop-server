"""The address of the client behind a reverse proxy."""
import ipaddress

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_networks(text: str) -> list[Network]:
    """Parses "10.0.0.5, 172.18.0.0/16". An entry that is no address is an error."""
    networks = []
    for item in text.split(","):
        item = item.strip()
        if item:
            networks.append(ipaddress.ip_network(item, strict=False))
    return networks


def _address(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(text.strip())
    except ValueError:
        return None


def _trusted(address, networks: list[Network]) -> bool:
    return any(address.version == net.version and address in net for net in networks)


def client_address(peer: str | None, forwarded_for: str | None, trusted: list[Network]) -> str:
    """The address a request comes from.

    X-Forwarded-For is only believed when the peer is a trusted proxy. The
    chain is read from the right, skipping trusted proxies; the first other
    address is the client. Whatever stands further left was written by the
    client and counts for nothing. A chain that cannot be read gives the peer.
    """
    if not peer:
        return "unknown"

    peer_address = _address(peer)
    if peer_address is None or not _trusted(peer_address, trusted):
        return peer

    for hop in reversed([h for h in (forwarded_for or "").split(",") if h.strip()]):
        address = _address(hop)
        if address is None:
            return peer
        if not _trusted(address, trusted):
            return str(address)

    return peer
