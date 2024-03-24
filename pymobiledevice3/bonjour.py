import asyncio
import dataclasses
import sys
from socket import AF_INET, AF_INET6, inet_ntop
from typing import List, Mapping, Optional

from ifaddr import get_adapters
from zeroconf import IPVersion, ServiceListener, ServiceStateChange, Zeroconf
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

REMOTEPAIRING_SERVICE_NAME = '_remotepairing-manual-pairing._tcp.local.'
MOBDEV2_SERVICE_NAME = '_apple-mobdev2._tcp.local.'
REMOTED_SERVICE_NAME = '_remoted._tcp.local.'
DEFAULT_BONJOUR_TIMEOUT = 1 if sys.platform != 'win32' else 2  # On Windows, it takes longer to get the addresses


@dataclasses.dataclass
class BonjourAnswer:
    properties: Mapping[bytes, bytes]
    ips: List[str]
    port: int


class BonjourListener(ServiceListener):
    def __init__(self, ip: str):
        super().__init__()
        self.properties: Mapping[bytes, bytes] = {}
        self.ip = ip
        self.port: Optional[int] = None
        self.addresses: List[str] = []
        self.queue: asyncio.Queue = asyncio.Queue()
        self.querying_task: Optional[asyncio.Task] = asyncio.create_task(self.query_addresses())

    def async_on_service_state_change(
            self, zeroconf: Zeroconf, service_type: str, name: str, state_change: ServiceStateChange) -> None:
        self.queue.put_nowait((zeroconf, service_type, name, state_change))

    async def query_addresses(self) -> None:
        zeroconf, service_type, name, state_change = await self.queue.get()
        service_info = AsyncServiceInfo(service_type, name)
        await service_info.async_request(zeroconf, 3000)
        ipv4 = [inet_ntop(AF_INET, address.packed) for address in
                service_info.ip_addresses_by_version(IPVersion.V4Only)]
        ipv6 = []
        if '%' in self.ip:
            ipv6 = [inet_ntop(AF_INET6, address.packed) + '%' + self.ip.split('%')[1] for address in
                    service_info.ip_addresses_by_version(IPVersion.V6Only)]
        self.addresses = ipv4 + ipv6
        self.properties = service_info.properties
        self.port = service_info.port

    async def close(self) -> None:
        self.querying_task.cancel()
        try:
            await self.querying_task
        except asyncio.CancelledError:
            pass


@dataclasses.dataclass
class BonjourQuery:
    zc: AsyncZeroconf
    service_browser: AsyncServiceBrowser
    listener: BonjourListener


def get_ipv6_ips() -> List[str]:
    ips = []
    if sys.platform == 'win32':
        # TODO: verify on windows
        ips = [f'{adapter.ips[0].ip[0]}%{adapter.ips[0].ip[2]}' for adapter in get_adapters() if adapter.ips[0].is_IPv6]
    else:
        for adapter in get_adapters():
            for ip in adapter.ips:
                if not ip.is_IPv6:
                    continue
                if ip.ip[0] in ('::1', 'fe80::1'):
                    # skip localhost
                    continue
                ips.append(f'{ip.ip[0]}%{adapter.nice_name}')
    return ips


def query_bonjour(service_name: str, ip: str) -> BonjourQuery:
    aiozc = AsyncZeroconf(interfaces=[ip])
    listener = BonjourListener(ip)
    service_browser = AsyncServiceBrowser(aiozc.zeroconf, [service_name],
                                          handlers=[listener.async_on_service_state_change])
    return BonjourQuery(aiozc, service_browser, listener)


async def browse(service_name: str, ips: List[str], timeout: float = DEFAULT_BONJOUR_TIMEOUT) -> List[BonjourAnswer]:
    bonjour_queries = [query_bonjour(service_name, adapter) for adapter in ips]
    answers = []
    await asyncio.sleep(timeout)
    for bonjour_query in bonjour_queries:
        if bonjour_query.listener.addresses:
            answer = BonjourAnswer(bonjour_query.listener.properties, bonjour_query.listener.addresses,
                                   bonjour_query.listener.port)
            if answer not in answers:
                answers.append(answer)
        await bonjour_query.listener.close()
        await bonjour_query.service_browser.async_cancel()
        await bonjour_query.zc.async_close()
    return answers


async def browse_ipv6(service_name: str, timeout: float = DEFAULT_BONJOUR_TIMEOUT) -> List[BonjourAnswer]:
    return await browse(service_name, get_ipv6_ips(), timeout=timeout)


async def browse_ipv4(service_name: str, timeout: float = DEFAULT_BONJOUR_TIMEOUT) -> List[BonjourAnswer]:
    ips = []
    for adapter in get_adapters():
        for ip in adapter.ips:
            if ip.ip == '127.0.0.1':
                continue
            if not ip.is_IPv4:
                continue
            ips.append(ip.ip)
    return await browse(service_name, ips, timeout=timeout)


async def browse_remoted(timeout: float = DEFAULT_BONJOUR_TIMEOUT) -> List[BonjourAnswer]:
    return await browse_ipv6(REMOTED_SERVICE_NAME, timeout=timeout)


async def browse_mobdev2(timeout: float = DEFAULT_BONJOUR_TIMEOUT) -> List[BonjourAnswer]:
    return await browse_ipv4(MOBDEV2_SERVICE_NAME, timeout=timeout)


async def browse_remotepairing(timeout: float = DEFAULT_BONJOUR_TIMEOUT) -> List[BonjourAnswer]:
    return await browse_ipv4(REMOTEPAIRING_SERVICE_NAME, timeout=timeout)
