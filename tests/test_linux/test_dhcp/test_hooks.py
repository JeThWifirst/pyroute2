import asyncio
import json
import logging

import pytest
from test_dhcp.conftest import ipv4_addrs

from pyroute2.dhcp import hooks
from pyroute2.dhcp.leases import JSONFileLease
from pyroute2.iproute.linux import AsyncIPRoute

FAKE_LEASE = {
    'ack': {
        'op': 2,
        'htype': 1,
        'hlen': 6,
        'hops': 0,
        'xid': 1323206580,
        'secs': 0,
        'flags': 32768,
        'ciaddr': '0.0.0.0',
        'yiaddr': '192.168.112.73',
        'siaddr': '192.168.112.1',
        'giaddr': '0.0.0.0',
        'chaddr': '72:c1:55:6f:76:83',
        'sname': '',
        'file': '',
        'cookie': '63:82:53',
        'options': {
            'message_type': 5,
            'server_id': '192.168.112.1',
            'lease_time': 120,
            'renewal_time': 60,
            'rebinding_time': 105,
            'subnet_mask': '255.255.255.0',
            'broadcast_address': '192.168.112.255',
            'router': ['192.168.112.1'],
            'name_server': ['192.168.112.1'],
        },
    },
    'interface': '<SET ME>',
    'server_mac': '2e:7e:7d:8e:5f:5f',
    'obtained': 1738249608.073041,
}


@pytest.fixture
def fake_lease(dummy_iface: tuple[int, str]) -> JSONFileLease:
    '''Fixture that returns a fake lease loaded from disk.'''
    ifname = dummy_iface[1]
    raw_lease = FAKE_LEASE.copy()
    raw_lease['interface'] = ifname
    JSONFileLease._get_path(ifname).write_text(json.dumps(raw_lease))
    lease = JSONFileLease.load(ifname)
    assert lease
    return lease


@pytest.mark.asyncio
async def test_add_and_remove_ip_hooks(
    fake_lease: JSONFileLease,
    dummy_iface: tuple[int, str],
    caplog: pytest.LogCaptureFixture,
):
    '''Test the hooks that add & remove an address from an interface.'''
    caplog.set_level(logging.INFO, logger='pyroute2.dhcp.hooks')

    # call the hook that adds the IP address to the dummy interface
    await hooks.configure_ip(lease=fake_lease)
    ifindex = dummy_iface[0]
    # check the ip addr & broadcast addr have ben set
    assert len(addrs := await ipv4_addrs(ifindex)) == 1
    addr = addrs[0]
    assert addr.get('IFA_ADDRESS') == fake_lease.ip
    assert addr.get('IFA_BROADCAST') == fake_lease.broadcast_address

    # call the hooks that removes the IP address
    await hooks.remove_ip(lease=fake_lease)
    # check the interface has no address anymore
    assert len(await ipv4_addrs(ifindex)) == 0

    assert caplog.messages == [
        f'Adding {fake_lease.ip}/{fake_lease.subnet_mask}'
        f' to {fake_lease.interface}',
        f'Removing {fake_lease.ip}/{fake_lease.subnet_mask}'
        f' from {fake_lease.interface}',
    ]


@pytest.mark.asyncio
async def test_configure_ip_missing_broadcast_addr(
    fake_lease: JSONFileLease,
    dummy_iface: tuple[int, str],
    caplog: pytest.LogCaptureFixture,
):
    '''The configure_ip hook mustn't crash when broadcast addr is missing.'''
    caplog.set_level(logging.DEBUG, logger='pyroute2.dhcp.hooks')

    del fake_lease.ack['options']['broadcast_address']
    await hooks.configure_ip(fake_lease)
    assert caplog.messages == [
        f'Adding {fake_lease.ip}/{fake_lease.subnet_mask}'
        f' to {fake_lease.interface}',
        'Lease does not set <Option.BROADCAST_ADDRESS: 28>',
    ]
    # check the ip addr has been set, but no broadcast addr
    assert len(addrs := await ipv4_addrs(dummy_iface[0])) == 1
    addr = addrs[0]
    assert addr.get('IFA_ADDRESS') == fake_lease.ip
    assert addr.get('IFA_BROADCAST') is None


@pytest.mark.skip(reason='Messes up routing; we need to use a netns')
@pytest.mark.asyncio
async def test_add_and_remove_gw_hooks(
    fake_lease: JSONFileLease,
    dummy_iface: tuple[int, str],
    caplog: pytest.LogCaptureFixture,
):
    '''Test the hooks that add & remove the default gw for a lease.'''
    caplog.set_level(logging.INFO, logger='pyroute2.dhcp.hooks')
    ifindex = dummy_iface[0]
    async with AsyncIPRoute() as ipr:
        await ipr.addr(
            'add',
            index=ifindex,
            address=fake_lease.ip,
            mask=fake_lease.subnet_mask,
        )
        await hooks.add_default_gw(lease=fake_lease)
        routes = await ipr.route('get', dst='1.2.3.4')
        assert len(routes) == 1
        assert routes[0].get('RTA_DST') == '1.2.3.4'
        assert routes[0].get('RTA_OIF') == ifindex
        assert routes[0].get('RTA_PREFSRC') == fake_lease.ip
        await hooks.remove_default_gw(lease=fake_lease)
        routes = await ipr.route('get', dst='1.2.3.4')
        assert routes[0].get('RTA_OIF') != ifindex

    assert caplog.messages == [
        f'Adding {fake_lease.default_gateway} '
        f'as default route through {fake_lease.interface}',
        f'Removing {fake_lease.default_gateway} as default route',
    ]


@pytest.mark.asyncio
async def test_remove_gw_already_removed(
    fake_lease: JSONFileLease, caplog: pytest.LogCaptureFixture
):
    '''Removing the default gw must not crash when it doesn't exist.'''
    caplog.set_level(logging.INFO, logger='pyroute2.dhcp.hooks')
    await hooks.remove_default_gw(lease=fake_lease)
    assert caplog.messages == [
        f'Removing {fake_lease.default_gateway} as default route',
        'Default route was already removed by another process',
    ]


@pytest.mark.asyncio
async def test_hook_timeout(
    fake_lease: JSONFileLease, caplog: pytest.LogCaptureFixture
):
    '''Hooks that exceed the timeout cause an error log but no crash.'''
    caplog.set_level(logging.ERROR, logger='pyroute2.dhcp.hooks')

    @hooks.hook(hooks.Trigger.BOUND)
    async def sleepy_hook(lease):
        await asyncio.sleep(10)

    await hooks.run_hooks(
        hooks=[sleepy_hook],
        lease=fake_lease,
        trigger=hooks.Trigger.BOUND,
        timeout=0.1,
    )
    assert caplog.messages == ["Hook 'sleepy_hook' timed out"]


@pytest.mark.asyncio
async def test_failing_hook(
    fake_lease: JSONFileLease, caplog: pytest.LogCaptureFixture
):
    '''Hooks that raise an exception cause an error log but no crash.'''
    caplog.set_level(logging.ERROR, logger='pyroute2.dhcp.hooks')

    @hooks.hook(hooks.Trigger.BOUND)
    async def failing_hook(lease):
        raise RuntimeError('boom')

    await hooks.run_hooks(
        hooks=[failing_hook], lease=fake_lease, trigger=hooks.Trigger.BOUND
    )
    assert caplog.messages == [
        "Hook failing_hook failed: RuntimeError('boom')"
    ]
