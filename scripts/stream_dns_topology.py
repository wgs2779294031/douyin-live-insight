"""Identify DNS servers on connected physical Windows default uplinks (no DNS I/O)."""

import ctypes as ct
import ipaddress
import sys
import sysconfig


_U8, _U16, _U32, _U64 = ct.c_uint8, ct.c_uint16, ct.c_uint32, ct.c_uint64
_I32, _PTR = ct.c_int32, ct.c_void_p
_AF_UNSPEC, _AF_INET, _AF_INET6 = 0, 2, 23
_ERROR_BUFFER_OVERFLOW, _ERROR_NO_DATA = 111, 232
_MAX_RECORDS = 65_536
_MAX_ADAPTER_BYTES = 1_048_576
_MAX_DNS_SERVERS = 512


class _SocketIn(ct.Structure):
    _fields_ = [("family", _U16), ("port", _U16), ("address", _U32),
                ("reserved", _U8 * 8)]


class _SocketIn6(ct.Structure):
    _fields_ = [("family", _U16), ("port", _U16), ("flow", _U32),
                ("address", _U8 * 16), ("scope", _U32)]


class _SocketInet(ct.Union):
    _fields_ = [("ipv4", _SocketIn), ("ipv6", _SocketIn6), ("family", _U16)]


class _Prefix(ct.Structure):
    _fields_ = [("address", _SocketInet), ("length", _U8)]


class _Route(ct.Structure):
    _fields_ = [("luid", _U64), ("index", _U32), ("destination", _Prefix),
                ("next_hop", _SocketInet), ("site_prefix_length", _U8),
                ("valid_lifetime", _U32), ("preferred_lifetime", _U32), ("metric", _U32),
                ("protocol", _I32), ("loopback", _U8), ("autoconfigure", _U8),
                ("publish", _U8), ("immortal", _U8), ("age", _U32), ("origin", _I32)]


class _RouteTable(ct.Structure):
    _fields_ = [("count", _U32), ("rows", _Route * 1)]


class _Guid(ct.Structure):
    _fields_ = [("data1", _U32), ("data2", _U16), ("data3", _U16), ("data4", _U8 * 8)]


class _Interface(ct.Structure):
    _fields_ = [("luid", _U64), ("index", _U32), ("guid", _Guid),
                ("alias", _U16 * 257), ("description", _U16 * 257),
                ("physical_address_length", _U32), ("physical_address", _U8 * 32),
                ("permanent_physical_address", _U8 * 32), ("mtu", _U32),
                ("type", _U32), ("tunnel_type", _I32), ("media_type", _I32),
                ("physical_medium", _I32), ("access_type", _I32), ("direction_type", _I32),
                ("operational_flags", _U8), ("operational_status", _I32),
                ("admin_status", _I32), ("media_connect_state", _I32), ("network_guid", _Guid),
                ("connection_type", _I32), ("link_and_traffic_statistics", _U64 * 20)]


class _IpInterface(ct.Structure):
    _fields_ = [("family", _U16), ("luid", _U64), ("index", _U32),
                ("maximum_reassembly_size", _U32), ("interface_identifier", _U64),
                ("minimum_router_advertisement_interval", _U32),
                ("maximum_router_advertisement_interval", _U32),
                ("advertising_enabled", _U8), ("forwarding_enabled", _U8),
                ("weak_host_send", _U8), ("weak_host_receive", _U8),
                ("use_automatic_metric", _U8), ("use_neighbor_unreachability_detection", _U8),
                ("managed_address_configuration_supported", _U8),
                ("other_stateful_configuration_supported", _U8), ("advertise_default_route", _U8),
                ("router_discovery_behavior", _I32), ("dad_transmits", _U32),
                ("base_reachable_time", _U32), ("retransmit_time", _U32),
                ("path_mtu_discovery_timeout", _U32), ("link_local_address_behavior", _I32),
                ("link_local_address_timeout", _U32), ("zone_indices", _U32 * 16),
                ("site_prefix_length", _U32), ("metric", _U32), ("mtu", _U32),
                ("connected", _U8), ("supports_wake_up_patterns", _U8),
                ("supports_neighbor_discovery", _U8), ("supports_router_discovery", _U8),
                ("reachable_time", _U32), ("transmit_offload", _U8), ("receive_offload", _U8),
                ("disable_default_routes", _U8)]


class _Adapter(ct.Structure):
    # GetAdaptersAddresses allocates the full SDK records inside the caller's
    # buffer. We only read the common Windows 10/11 prefix through Luid.
    _fields_ = [("length", _U32), ("ipv4_index", _U32), ("next", _PTR),
                ("name", _PTR), ("first_unicast", _PTR), ("first_anycast", _PTR),
                ("first_multicast", _PTR), ("first_dns", _PTR), ("dns_suffix", _PTR),
                ("description", _PTR), ("friendly_name", _PTR), ("physical_address", _U8 * 8),
                ("physical_address_length", _U32), ("flags", _U32), ("mtu", _U32),
                ("type", _U32), ("operational_status", _I32), ("ipv6_index", _U32),
                ("zone_indices", _U32 * 16), ("first_prefix", _PTR),
                ("transmit_link_speed", _U64), ("receive_link_speed", _U64),
                ("first_wins", _PTR), ("first_gateway", _PTR), ("ipv4_metric", _U32),
                ("ipv6_metric", _U32), ("luid", _U64)]


class _DnsNode(ct.Structure):
    _fields_ = [("length", _U32), ("reserved", _U32), ("next", _PTR),
                ("socket_address", _PTR), ("socket_length", _I32)]


def _check_layout():
    # Windows SDK 10.0.22621.0, native x64 alignment. ctypes must not write a
    # full SDK row into a smaller or packed Python buffer.
    actual = (
        (ct.sizeof(_SocketIn), ct.sizeof(_SocketIn6), ct.sizeof(_Prefix)),
        (ct.sizeof(_Route), _Route.destination.offset, _Route.metric.offset, _RouteTable.rows.offset),
        (ct.sizeof(_Interface), _Interface.operational_flags.offset, _Interface.operational_status.offset),
        (ct.sizeof(_IpInterface), _IpInterface.metric.offset, _IpInterface.connected.offset),
        (ct.sizeof(_Adapter), _Adapter.first_dns.offset, _Adapter.ipv6_index.offset, _Adapter.luid.offset),
        (ct.sizeof(_DnsNode), _DnsNode.socket_address.offset, _DnsNode.socket_length.offset),
    )
    if actual != ((16, 28, 32), (104, 12, 84, 8), (1352, 1152, 1156),
                  (168, 148, 156), (232, 48, 108, 224), (32, 16, 24)):
        raise ValueError("unexpected Windows IP Helper ABI")


def _ip_helper():
    if (sys.platform != "win32" or sysconfig.get_platform() != "win-amd64"
            or sys.getwindowsversion().major != 10 or ct.sizeof(_PTR) != 8):
        raise OSError("Windows 10 or 11 x64 is required")
    _check_layout()
    api = ct.WinDLL("iphlpapi.dll", winmode=0x800)  # LOAD_LIBRARY_SEARCH_SYSTEM32
    bindings = (
        ("GetIpForwardTable2", [_U16, ct.POINTER(_PTR)], _U32),
        ("FreeMibTable", [_PTR], None),
        ("GetIfEntry2Ex", [_I32, ct.POINTER(_Interface)], _U32),
        ("InitializeIpInterfaceEntry", [ct.POINTER(_IpInterface)], None),
        ("GetIpInterfaceEntry", [ct.POINTER(_IpInterface)], _U32),
        ("GetAdaptersAddresses", [_U32, _U32, _PTR, _PTR, ct.POINTER(_U32)], _U32),
    )
    for name, arguments, result in bindings:
        try:
            function = getattr(api, name)
        except AttributeError:
            raise OSError(f"IP Helper API unavailable: {name}") from None
        function.argtypes, function.restype = arguments, result
    return api


def _check_status(status, function):
    if status:
        raise OSError(status, function)


def _default_family(prefix):
    if prefix.length == 0:
        family = prefix.address.family
        if family == _AF_INET and prefix.address.ipv4.address == 0:
            return family
        if family == _AF_INET6 and not any(prefix.address.ipv6.address):
            return family
    return None


def _uplinks(api):
    table = _PTR()
    try:
        status = api.GetIpForwardTable2(_AF_UNSPEC, ct.byref(table))
        if status == _ERROR_NO_DATA:
            return []
        _check_status(status, "GetIpForwardTable2")
        if not table.value or table.value % ct.alignment(_RouteTable):
            raise ValueError("invalid default route table")
        header = _RouteTable.from_address(table.value)
        count = header.count
        if count > _MAX_RECORDS:
            raise ValueError("default route table exceeds the record limit")
        routes = (_Route * count).from_address(table.value + _RouteTable.rows.offset)
        paths = []
        physical = {}
        connected = {}
        for route in routes:
            family = _default_family(route.destination)
            if family is None:
                continue
            if not route.luid or not route.index:
                raise ValueError("invalid default route interface")
            if route.luid not in physical:
                interface = _Interface()
                interface.luid = route.luid
                # MibIfEntryNormalWithoutStatistics. The lowest BOOLEAN bit
                # is HardwareInterface. IfOperStatusUp is 1.
                _check_status(api.GetIfEntry2Ex(2, ct.byref(interface)), "GetIfEntry2Ex")
                if interface.luid != route.luid:
                    raise ValueError("default route interface changed")
                physical[route.luid] = bool(interface.operational_flags & 1) and interface.operational_status == 1
            if not physical[route.luid]:
                continue
            key = (family, route.luid)
            if key not in connected:
                ip = _IpInterface()
                api.InitializeIpInterfaceEntry(ct.byref(ip))
                ip.family, ip.luid = key
                _check_status(api.GetIpInterfaceEntry(ct.byref(ip)), "GetIpInterfaceEntry")
                if (ip.family, ip.luid) != key:
                    raise ValueError("default route IP interface changed")
                connected[key] = ip.metric if ip.connected else None
            metric = connected[key]
            if metric is not None:
                paths.append((route.metric + metric, route.index, route.luid))
        return list(dict.fromkeys(luid for _, _, luid in sorted(paths)))
    finally:
        if table.value:
            api.FreeMibTable(table)


def _adapters_buffer(api):
    # Recommended initial GetAdaptersAddresses buffer is about 15 KB.
    # Skip unicast, anycast, multicast, and friendly names, keeping DNS servers.
    flags = 0x01 | 0x02 | 0x04 | 0x20
    requested = _U32(15_000)
    for _ in range(3):
        if requested.value > _MAX_ADAPTER_BYTES:
            break
        buffer = ct.create_string_buffer(requested.value)
        status = api.GetAdaptersAddresses(_AF_UNSPEC, flags, None, buffer, ct.byref(requested))
        if status == _ERROR_NO_DATA:
            return None
        if status != _ERROR_BUFFER_OVERFLOW:
            _check_status(status, "GetAdaptersAddresses")
            return buffer
    raise OSError(_ERROR_BUFFER_OVERFLOW, "GetAdaptersAddresses")


def _checked_view(buffer, address, structure, length=None):
    base = ct.addressof(buffer)
    size = ct.sizeof(structure)
    length = size if length is None else length
    if (not isinstance(address, int) or address < base or address % ct.alignment(structure)
            or length < size or address - base > ct.sizeof(buffer) - length):
        raise ValueError("invalid address inside Windows adapter buffer")
    return structure.from_address(address)


def _linked(buffer, start, structure):
    seen = set()
    address = start
    while address:
        if address in seen or len(seen) >= _MAX_RECORDS:
            raise ValueError("invalid Windows adapter list")
        seen.add(address)
        item = _checked_view(buffer, address, structure)
        _checked_view(buffer, address, structure, item.length)
        yield item
        address = item.next


def _resolver_address(buffer, adapter, node):
    family = _checked_view(buffer, node.socket_address, _U16, node.socket_length).value
    if family == _AF_INET:
        address = _checked_view(buffer, node.socket_address, _SocketIn, node.socket_length)
        return family, str(ipaddress.IPv4Address(address.address.to_bytes(4, "little")))
    if family == _AF_INET6:
        address = _checked_view(buffer, node.socket_address, _SocketIn6, node.socket_length)
        parsed = ipaddress.IPv6Address(bytes(address.address))
        scope = address.scope or (adapter.ipv6_index if parsed.is_link_local else 0)
        if parsed.is_link_local and not scope:
            raise ValueError("missing IPv6 link-local DNS scope")
        return family, (f"{parsed}%{scope}" if scope else str(parsed))
    raise ValueError("unknown DNS resolver address family")


def configured_uplink_dns():
    """Return only the configured DNS endpoints of connected physical default uplinks."""
    api = _ip_helper()
    uplinks = _uplinks(api)
    if not uplinks:
        return []
    buffer = _adapters_buffer(api)
    if buffer is None:
        return []
    qualifying = set(uplinks)
    adapters = {}
    for adapter in _linked(buffer, ct.addressof(buffer), _Adapter):
        if adapter.luid in qualifying:
            if adapter.luid in adapters:
                raise ValueError("duplicate default uplink adapter")
            adapters[adapter.luid] = adapter
    if qualifying != adapters.keys():
        raise ValueError("default uplink adapters changed during enumeration")
    resolvers = {}
    for luid in uplinks:
        adapter = adapters[luid]
        configured = {}
        for node in _linked(buffer, adapter.first_dns, _DnsNode):
            family, address = _resolver_address(buffer, adapter, node)
            configured[address] = family
            if len(configured) > _MAX_DNS_SERVERS:
                raise ValueError("configured DNS server count exceeds the limit")
        # Match the old Get-DnsClientServerAddress family row order per
        # interface without losing the configured order within either family.
        resolvers.update(dict.fromkeys(sorted(configured, key=configured.__getitem__)))
        if len(resolvers) > _MAX_DNS_SERVERS:
            raise ValueError("configured DNS server count exceeds the limit")
    return list(resolvers)
