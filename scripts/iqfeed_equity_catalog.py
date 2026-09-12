"""Read the complete official symbol file and retain its equity instruments.

No provider connections, subscriptions, ticker rewriting or trading policy.
Non-equity rows are consumed and included in the source digest/count, not confused
with missing equity coverage. Bounds apply to the whole resource; never top-N.
"""
from dataclasses import dataclass
import hashlib

SOURCE_URL = 'https://www.iqfeed.net/downloads/download_file.cfm?type=mktsymbols'
HEADER = b'SYMBOL\tDESCRIPTION\tEXCHANGE\tLISTED MARKET\tSECURITY TYPE\tSIC\tFRONTMONTH\tNAICS'


@dataclass(frozen=True)
class EquityListing:
    symbol: str
    description: str
    exchange: str
    listed_market: str
    sic: str
    frontmonth: str
    naics: str
    source_line_sha256: str


@dataclass(frozen=True)
class EquityCatalog:
    source_url: str
    observed_ns: int
    source_text_sha256: str
    source_bytes: int
    source_rows: int
    equities: tuple[EquityListing, ...]
    provider_published_at: None = None
    current_tick_coverage_certified: bool = False


def read_catalog(stream, *, observed_ns, max_bytes, max_equities):
    if any(type(v) is not int or v <= 0 for v in (observed_ns, max_bytes, max_equities)):
        raise ValueError('iqfeed_catalog_capacity_or_clock_invalid')
    digest, size, rows, equities, keys = hashlib.sha256(), 0, 0, [], set()
    header_seen = False
    # readline's bound prevents one malformed line allocating the entire input.
    while True:
        line = stream.readline(max_bytes-size+1)
        if not line:
            break
        if type(line) is not bytes:
            raise ValueError('iqfeed_catalog_binary_stream_required')
        size += len(line)
        if size > max_bytes:
            raise ValueError('iqfeed_catalog_byte_capacity')
        digest.update(line)
        payload = line.rstrip(b'\r\n')
        if not header_seen:
            if payload != HEADER:
                raise ValueError('iqfeed_catalog_header_invalid')
            header_seen = True
            continue
        parts = payload.split(b'\t')
        if len(parts) != 8:
            raise ValueError('iqfeed_catalog_row_shape_invalid')
        rows += 1
        if parts[4] != b'EQUITY':
            continue
        symbol, exchange, listed = (parts[i].decode('ascii') for i in (0, 2, 3))
        if any(not v or v != v.strip().upper() or any(ord(c) < 33 or ord(c) > 126 for c in v)
               for v in (symbol, exchange, listed)):
            raise ValueError('iqfeed_catalog_identity_invalid')
        key = (symbol, exchange, listed)
        if key in keys:
            raise ValueError('iqfeed_catalog_duplicate_instrument')
        keys.add(key)
        if len(keys) > max_equities:
            raise ValueError('iqfeed_catalog_equity_capacity')
        # Latin-1 is a reversible byte representation, not a claim about the
        # provider's human-name encoding. Mapping never uses fuzzy names.
        equities.append(EquityListing(symbol, parts[1].decode('latin-1'), exchange, listed,
            *(parts[i].decode('latin-1') for i in (5, 6, 7)), hashlib.sha256(line).hexdigest()))
    if not header_seen:
        raise ValueError('iqfeed_catalog_header_missing')
    return EquityCatalog(SOURCE_URL, observed_ns, digest.hexdigest(), size, rows,
                         tuple(sorted(equities, key=lambda r: (r.symbol, r.exchange, r.listed_market))))
