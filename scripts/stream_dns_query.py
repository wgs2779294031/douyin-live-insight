"""Query a nominated Windows DNS server through the native DNS client."""
from __future__ import annotations

import ctypes
import ipaddress
import re
import struct
import sys


_LABEL = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?")
_AF_INET = 2
_AF_INET6 = 23
_DNS_QUERY_VERSION1 = 1
# WIRE_ONLY | NO_HOSTS_FILE | NO_NETBT | NO_MULTICAST | TREAT_AS_FQDN.
_QUERY_OPTIONS = 0x100 | 0x40 | 0x80 | 0x800 | 0x1000
_DNS_FREE_RECORD_LIST = 1
_DNS_SECTION_MASK = 0b11
_DNS_SECTION_ANSWER = 1
_ERROR_INVALID_PARAMETER = 87


class _DNSAddress(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("sockaddr", ctypes.c_ubyte * 32),
        ("reserved", ctypes.c_uint32 * 8),
    ]


class _DNSAddressArray(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("max_count", ctypes.c_uint32),
        ("addr_count", ctypes.c_uint32),
        ("tag", ctypes.c_uint32),
        ("family", ctypes.c_uint16),
        ("word_reserved", ctypes.c_uint16),
        ("flags", ctypes.c_uint32),
        ("match_flag", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 2),
        ("addresses", _DNSAddress * 1),
    ]


class _DNSRecord(ctypes.Structure):
    pass


class _DNSRecordData(ctypes.Union):
    _fields_ = [
        ("ipv4", ctypes.c_ubyte * 4),
        ("ipv6", ctypes.c_ubyte * 16),
        # DNS_RECORDW.Data is an SDK union that can contain pointers and QWORDs.
        ("alignment", ctypes.c_uint64),
    ]


_DNSRecord._fields_ = [
    ("next", ctypes.POINTER(_DNSRecord)),
    ("name", ctypes.c_void_p),
    ("type", ctypes.c_uint16),
    ("data_length", ctypes.c_uint16),
    ("flags", ctypes.c_uint32),
    ("ttl", ctypes.c_uint32),
    ("reserved", ctypes.c_uint32),
    ("data", _DNSRecordData),
]


class _DNSQueryRequest(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_uint32),
        ("query_name", ctypes.c_wchar_p),
        ("query_type", ctypes.c_uint16),
        ("query_options", ctypes.c_uint64),
        ("server_list", ctypes.POINTER(_DNSAddressArray)),
        ("interface_index", ctypes.c_uint32),
        ("completion_callback", ctypes.c_void_p),
        ("context", ctypes.c_void_p),
    ]


class _DNSQueryResult(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_uint32),
        ("status", ctypes.c_int32),
        ("query_options", ctypes.c_uint64),
        ("records", ctypes.POINTER(_DNSRecord)),
        ("reserved", ctypes.c_void_p),
    ]


def _validate_hostname(hostname: str) -> None:
    if not isinstance(hostname, str):
        raise ValueError("invalid_dns_hostname")
    name = hostname.removesuffix(".")
    if len(name) > 253 or any(not _LABEL.fullmatch(label) for label in name.split(".")):
        raise ValueError("invalid_dns_hostname")
    try:
        ipaddress.IPv4Address(name)
    except ipaddress.AddressValueError:
        return
    raise ValueError("invalid_dns_hostname")


def _server_list(resolver: str) -> _DNSAddressArray:
    if not isinstance(resolver, str) or len(resolver) > 56:
        raise ValueError("invalid_dns_resolver")
    literal, separator, scope_text = resolver.partition("%")
    try:
        address = ipaddress.ip_address(literal)
    except ValueError:
        raise ValueError("invalid_dns_resolver") from None
    if address.is_unspecified or address.is_multicast:
        raise ValueError("invalid_dns_resolver")

    scope = 0
    if separator:
        if address.version != 6 or not scope_text.isascii() or not scope_text.isdecimal():
            raise ValueError("invalid_dns_resolver_scope")
        scope = int(scope_text)
        if not 0 < scope <= 0xFFFFFFFF:
            raise ValueError("invalid_dns_resolver_scope")
    if address.version == 6 and address.is_link_local and scope == 0:
        raise ValueError("invalid_dns_resolver_scope")

    if address.version == 4:
        family = _AF_INET
        sockaddr = struct.pack("<HH", family, 0) + address.packed
    else:
        family = _AF_INET6
        sockaddr = (struct.pack("<HHI", family, 0, 0) +
                    address.packed + struct.pack("<I", scope))

    # Match the SDK sample. DNS supplies the default port for the sockaddr.
    # Leave the outer DNS_ADDR_ARRAY family unset for both address families.
    servers = _DNSAddressArray(max_count=1, addr_count=1)
    servers.addresses[0].sockaddr[:len(sockaddr)] = sockaddr
    return servers


def _dns_functions():
    if sys.platform != "win32":
        raise OSError("windows_dns_api_unavailable")
    try:
        library = ctypes.WinDLL("dnsapi.dll", winmode=0x800)  # LOAD_LIBRARY_SEARCH_SYSTEM32
        query = library.DnsQueryEx
        free = library.DnsFree  # The SDK's DnsRecordListFree uses DnsFree for this target.
    except (AttributeError, OSError):
        raise OSError("windows_dns_api_unavailable") from None
    query.argtypes = [ctypes.POINTER(_DNSQueryRequest), ctypes.POINTER(_DNSQueryResult), ctypes.c_void_p]
    query.restype = ctypes.c_int32
    free.argtypes = [ctypes.c_void_p, ctypes.c_int]
    free.restype = None
    return query, free


def query_dns(hostname: str, resolver: str, record_type: int) -> list[tuple[str, int]]:
    """Return address and TTL pairs for A (1) or AAAA (28) answers.

    The caller owns the deadline. Windows resolves CNAMEs and returns the
    answer records from the nominated resolver.
    """
    if type(record_type) is not int or record_type not in (1, 28):
        raise ValueError("invalid_dns_record_type")
    _validate_hostname(hostname)
    servers = _server_list(resolver)
    query, free = _dns_functions()
    request = _DNSQueryRequest(
        version=_DNS_QUERY_VERSION1,
        query_name=hostname,
        query_type=record_type,
        query_options=_QUERY_OPTIONS,
        server_list=ctypes.pointer(servers),
    )
    result = _DNSQueryResult(version=_DNS_QUERY_VERSION1)
    try:
        status = query(ctypes.byref(request), ctypes.byref(result), None)
        if _ERROR_INVALID_PARAMETER in (status, result.status):
            raise OSError("windows_dns_query_parameters_rejected")
        if status or result.status:
            return []

        answers = []
        current = result.records
        while current:
            record = current.contents
            if record.type == record_type and record.flags & _DNS_SECTION_MASK == _DNS_SECTION_ANSWER:
                data = record.data.ipv4 if record_type == 1 else record.data.ipv6
                answers.append((str(ipaddress.ip_address(bytes(data))), record.ttl))
            current = record.next
        return answers
    finally:
        if result.records:
            free(result.records, _DNS_FREE_RECORD_LIST)
