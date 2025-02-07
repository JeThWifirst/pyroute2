'''Lease classes used by the dhcp client.'''

import abc
import json
import time
from dataclasses import asdict, dataclass, field
from logging import getLogger
from pathlib import Path
from secrets import SystemRandom
from typing import Generic, Optional, TypeVar

from pyroute2.common import dqn2int
from pyroute2.dhcp.dhcp4msg import dhcp4msg
from pyroute2.dhcp.enums.dhcp import Option

LOG = getLogger(__name__)

random = SystemRandom()


def _now() -> float:
    '''The current timestamp.'''
    return time.time()


class MissingOptionError(LookupError):
    '''Raised when trying to access a missing option in a lease.'''

    def __init__(self, opt: Option):
        super().__init__(f"Lease does not set {opt!r}")


LeaseOptionT = TypeVar('LeaseOptionT')


class LeaseOption(Generic[LeaseOptionT]):
    '''Descriptor to factorize properties on leases that read DHCP options.'''

    def __init__(self, opt: Option):
        '''Create a new instance that looks up the given option.'''
        self.opt = opt

    def __get__(
        self, obj: object, objtype: Optional[type] = None
    ) -> LeaseOptionT:
        '''Gets the option matching self.opt in the lease.'''
        assert isinstance(obj, Lease)
        opt_name = self.opt.name.lower()
        if opt_name not in obj.ack['options']:
            raise MissingOptionError(self.opt)
        opt_value = obj.ack['options'][opt_name]
        return opt_value


@dataclass
class Lease(abc.ABC):
    '''Represents a lease obtained through DHCP.'''

    # The DHCP ack sent by the server which allocated this lease
    ack: dhcp4msg
    # Name of the interface for which this lease was requested
    interface: str
    # MAC address of the server that allocated the lease
    server_mac: str
    # Timestamp of when this lease was obtained
    obtained: float = field(default_factory=_now)

    def _seconds_til_timer(self, timer_name: str) -> Optional[float]:
        '''The number of seconds to wait until the given timer expires.

        The value is fetched from options as `{timer_name}_time`.
        (lease -> lease_time, renewal -> renewal_time, ...)
        '''
        try:
            delta: int = self.ack['options'][f'{timer_name}_time']
            return self.obtained + delta - _now()
        except KeyError:
            return None

    @property
    def expired(self) -> bool:
        '''Whether this lease has expired (its expiration is in the past).

        When loading a persisted lease, this won't be correct if the clock
        has been adjusted since the lease was written.
        However the worst case scenario is that we send a REQUEST for it,
        get a NAK and restart from scratch.
        '''
        return self.expiration_in is not None and self.expiration_in <= 0

    @property
    def expiration_in(self) -> Optional[float]:
        '''The amount of seconds before the lease expires.

        Computed from the `lease_time` option.

        Can be negative if it's past due,
        or `None` if the server didn't give an expiration time.
        '''
        return self._seconds_til_timer('lease')

    @property
    def renewal_in(self) -> float:
        '''The amount of seconds before we have to renew the lease.

        Computed from the `renewal_time` option, defaults to ~.5 * lease exp.

        Can be negative if it's past due.
        '''
        if lease_renewal := self._seconds_til_timer('renewal'):
            return lease_renewal
        # RFC section 4.4.5 says we need a fuzzy value around 0.5
        # FIXME will crash if there is no lease time
        return self.expiration_in * random.uniform(0.4, 0.6)

    @property
    def rebinding_in(self) -> float:
        '''The amount of seconds before we have to rebind the lease.

        Computed from the `rebinding_time` option, defaults to ~.8 * lease exp.

        Can be negative if it's past due.
        '''
        if lease_rebinding := self._seconds_til_timer('rebinding'):
            return lease_rebinding
        # RFC section 4.4.5 says we need a fuzzy value around 0.875
        # FIXME will crash if there is no lease time
        return self.expiration_in * random.uniform(0.75, 0.90)

    @property
    def ip(self) -> str:
        '''The IP address assigned to the client.'''
        return self.ack['yiaddr']

    @property
    def prefixlen(self) -> int:
        '''The length of the subnet mask assigned to the client.'''
        return dqn2int(self.subnet_mask)

    @property
    def default_gateway(self) -> str:
        '''The default gateway for this interface.

        As mentioned by the RFC, the first router is the most prioritary.
        '''
        # TODO: unit test to make this crash
        return self.routers[0]

    # The subnet mask assigned to the client.
    subnet_mask = LeaseOption[str](Option.SUBNET_MASK)

    routers = LeaseOption[list[str]](Option.ROUTER)

    # The broadcast address for this network.
    broadcast_address = LeaseOption[str](Option.BROADCAST_ADDRESS)

    # The MTU for this interface.
    mtu = LeaseOption[int](Option.INTERFACE_MTU)

    name_servers = LeaseOption[list[str]](Option.NAME_SERVER)

    # The IP address of the server which allocated this lease.
    server_id = LeaseOption[str](Option.SERVER_ID)

    domain_name = LeaseOption[str](Option.DOMAIN_NAME)

    domain_search = LeaseOption[list[str]](Option.DOMAIN_SEARCH)

    @abc.abstractmethod
    def dump(self) -> None:
        '''Write a lease, i.e. to disk or to stdout.'''
        pass

    @classmethod
    @abc.abstractmethod
    def load(cls, interface: str) -> 'Optional[Lease]':
        '''Load an existing lease for an interface, if it exists.

        The lease is not checked for freshness, and will be None if no lease
        could be loaded.
        '''
        pass


class JSONStdoutLease(Lease):
    '''Just prints the lease to stdout when the client gets a new one.'''

    def dump(self) -> None:
        '''Writes the lease as json to stdout.'''
        print(json.dumps(asdict(self), indent=2))

    @classmethod
    def load(cls, interface: str) -> None:
        '''Does not do anything.'''
        return None


class JSONFileLease(Lease):
    '''Write and load the lease from a JSON file in the working directory.'''

    @classmethod
    def _get_lease_dir(cls) -> Path:
        '''Where to store the lease file, i.e. the working directory.'''
        return Path.cwd()

    @classmethod
    def _get_path(cls, interface: str) -> Path:
        '''The lease file, named after the interface.'''
        return (
            cls._get_lease_dir().joinpath(interface).with_suffix('.lease.json')
        )

    def dump(self) -> None:
        '''Dump the lease to a file.

        The lease file is named after the interface
        and written in the working directory.
        '''
        lease_path = self._get_path(self.interface)
        LOG.info('Writing lease for %s to %s', self.interface, lease_path)
        with lease_path.open('wt') as lf:
            json.dump(asdict(self), lf, indent=2)

    @classmethod
    def load(cls, interface: str) -> 'Optional[JSONFileLease]':
        '''Load the lease from a file.

        The lease file is named after the interface
        and read from the working directory.
        '''
        lease_path = cls._get_path(interface)
        try:
            with lease_path.open('rt') as lf:
                LOG.info('Loading lease for %s from %s', interface, lease_path)
                return cls(**json.load(lf))
        except FileNotFoundError:
            LOG.info('No existing lease at %s for %s', lease_path, interface)
            return None
        except TypeError as err:
            LOG.warning('Error loading lease: %s', err)
            return None
