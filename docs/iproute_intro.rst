.. _iproute_intro:

.. testsetup:: *

   from pyroute2 import config

   config.mock_netlink = True


RTNL classes
------------

.. autoclass:: pyroute2.AsyncIPRSocket

.. autoclass:: pyroute2.IPRSocket

.. autoclass:: pyroute2.AsyncIPRoute

.. autoclass:: pyroute2.IPRoute

.. autoclass:: pyroute2.NetNS
