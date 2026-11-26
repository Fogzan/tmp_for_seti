# Copyright 2012-2013 James McCauley
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at:
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
A shortest-path forwarding application (Dijkstra variant).

Standalone L2 switch that learns ethernet addresses across the entire
network and picks short paths between them using Dijkstra's algorithm.

Depends on openflow.discovery
Works with openflow.spanning_tree
"""

from pox.core import core
import pox.openflow.libopenflow_01 as of
from pox.lib.revent import *
from pox.lib.recoco import Timer
from collections import defaultdict
from pox.openflow.discovery import Discovery
from pox.lib.util import dpid_to_str
import time
import heapq

log = core.getLogger()

# Adjacency map.  [sw1][sw2] -> port from sw1 to sw2
adjacency = defaultdict(lambda:defaultdict(lambda:None))

# Switches we know of.  [dpid] -> Switch
switches = {}

# ethaddr -> (switch, port)
mac_map = {}

# [sw1][sw2] -> (distance, intermediate)
path_map = defaultdict(lambda:defaultdict(lambda:(None,None)))

# [sw1][sw2] -> previous node on shortest path (for Dijkstra)
_prev_map = defaultdict(lambda: defaultdict(lambda: None))

# Waiting path.  (dpid,xid)->WaitingPath
waiting_paths = {}

# Time to not flood in seconds
FLOOD_HOLDDOWN = 5

# Flow timeouts
FLOW_IDLE_TIMEOUT = 10
FLOW_HARD_TIMEOUT = 30

# How long is allowable to set up a path?
PATH_SETUP_TIME = 4

def _calc_paths ():
  """
  Модификация: алгоритм Дейкстры вместо Флойда-Уоршелла.
  """
  def dump ():
    for i in sws:
      for j in sws:
        a = path_map[i][j][0]
        if a is None: a = "*"
        print(a, end=" ")
      print()
      
  sws = list(switches.values())
  path_map.clear()
  
  # Инициализация графа
  graph = {}
  for sw_dpid in adjacency:
    graph[sw_dpid] = {}
    for port, neighbor_dpid in adjacency[sw_dpid].items():
      if neighbor_dpid is not None:
        graph[sw_dpid][neighbor_dpid] = 1

  # Алгоритм Дейкстры
  for src in graph:
    dist = {node: float('inf') for node in graph}
    dist[src] = 0
    prev = {node: None for node in graph}
    pq = [(0, src)]

    while pq:
      current_dist, u = heapq.heappop(pq)
      if current_dist > dist[u]:
        continue
      for v, weight in graph[u].items():
        distance = current_dist + weight
        if distance < dist[v]:
          dist[v] = distance
          prev[v] = u
          heapq.heappush(pq, (distance, v))

    # Запись в path_map
    for dst in graph:
      if src == dst:
        path_map[src][dst] = (0, None)
      elif dist[dst] != float('inf'):
        curr = dst
        while prev[curr] != src and prev[curr] is not None:
          curr = prev[curr]
        first_hop = curr
        path_map[src][dst] = (dist[dst], first_hop)
        
  # dump() # раскомментируйте, если хотите видеть матрицу путей

def _get_raw_path(src, dst):
    """
    Get a raw path (just a list of nodes to traverse).
    Uses _prev_map filled by Dijkstra.
    Returns intermediate nodes (without src and dst).
    """
    if len(path_map) == 0:
        _calc_paths()
    if src is dst:
        return []
    if path_map[src][dst][0] is None:
        return None

    # Reconstruct path src -> ... -> dst
    path = []
    cur = dst
    while cur != src and cur is not None:
        path.append(cur)
        cur = _prev_map[src][cur]
    if cur != src:
        return None
    path.reverse()
    return path[:-1]  # drop dst, _get_path will add it


def _check_path(p):
    """
    Make sure that a path is actually a string of nodes with connected ports
    returns True if path is valid
    """
    for a, b in zip(p[:-1], p[1:]):
        if adjacency[a[0]][b[0]] != a[2]:
            return False
        if adjacency[b[0]][a[0]] != b[1]:
            return False
    return True


def _get_path(src, dst, first_port, final_port):
    """
    Gets a cooked path -- a list of (node,in_port,out_port)
    """
    if src == dst:
        path = [src]
    else:
        path = _get_raw_path(src, dst)
        if path is None:
            return None
        path = [src] + path + [dst]

    r = []
    in_port = first_port
    for s1, s2 in zip(path[:-1], path[1:]):
        out_port = adjacency[s1][s2]
        r.append((s1, in_port, out_port))
        in_port = adjacency[s2][s1]
    r.append((dst, in_port, final_port))

    assert _check_path(r), "Illegal path!"
    return r


class WaitingPath (object):
    """
    A path which is waiting for its path to be established
    """
    def __init__(self, path, packet):
        self.expires_at = time.time() + PATH_SETUP_TIME
        self.path = path
        self.first_switch = path[0][0].dpid
        self.xids = set()
        self.packet = packet

        if len(waiting_paths) > 1000:
            WaitingPath.expire_waiting_paths()

    def add_xid(self, dpid, xid):
        self.xids.add((dpid, xid))
        waiting_paths[(dpid, xid)] = self

    @property
    def is_expired(self):
        return time.time() >= self.expires_at

    def notify(self, event):
        self.xids.discard((event.dpid, event.xid))
        if len(self.xids) == 0:
            if self.packet:
                log.debug("Sending delayed packet out %s"
                          % (dpid_to_str(self.first_switch),))
                msg = of.ofp_packet_out(data=self.packet,
                    action=of.ofp_action_output(port=of.OFPP_TABLE))
                core.openflow.sendToDPID(self.first_switch, msg)
            core.l2_dijkstra.raiseEvent(PathInstalled(self.path))

    @staticmethod
    def expire_waiting_paths():
        packets = set(waiting_paths.values())
        killed = 0
        for p in packets:
            if p.is_expired:
                killed += 1
                for entry in p.xids:
                    waiting_paths.pop(entry, None)
        if killed:
            log.error("%i paths failed to install" % (killed,))


class PathInstalled (Event):
    def __init__(self, path):
        self.path = path


class Switch (EventMixin):
    def __init__(self):
        self.connection = None
        self.ports = None
        self.dpid = None
        self._listeners = None
        self._connected_at = None

    def __repr__(self):
        return dpid_to_str(self.dpid)

    def _install(self, switch, in_port, out_port, match, buf=None):
        msg = of.ofp_flow_mod()
        msg.match = match
        msg.match.in_port = in_port
        msg.idle_timeout = FLOW_IDLE_TIMEOUT
        msg.hard_timeout = FLOW_HARD_TIMEOUT
        msg.actions.append(of.ofp_action_output(port=out_port))
        msg.buffer_id = buf
        switch.connection.send(msg)

    def _install_path(self, p, match, packet_in=None):
        wp = WaitingPath(p, packet_in)
        for sw, in_port, out_port in p:
            self._install(sw, in_port, out_port, match)
            msg = of.ofp_barrier_request()
            sw.connection.send(msg)
            wp.add_xid(sw.dpid, msg.xid)

    def install_path(self, dst_sw, last_port, match, event):
        p = _get_path(self, dst_sw, event.port, last_port)
        if p is None:
            log.warning("Can't get from %s to %s", match.dl_src, match.dl_dst)

            import pox.lib.packet as pkt

            if (match.dl_type == pkt.ethernet.IP_TYPE and
                    event.parsed.find('ipv4')):
                log.debug("Dest unreachable (%s -> %s)",
                          match.dl_src, match.dl_dst)

                from pox.lib.addresses import EthAddr
                e = pkt.ethernet()
                e.src = EthAddr(dpid_to_str(self.dpid))
                e.dst = match.dl_src
                e.type = e.IP_TYPE
                ipp = pkt.ipv4()
                ipp.protocol = ipp.ICMP_PROTOCOL
                ipp.srcip = match.nw_dst
                ipp.dstip = match.nw_src
                icmp = pkt.icmp()
                icmp.type = pkt.ICMP.TYPE_DEST_UNREACH
                icmp.code = pkt.ICMP.CODE_UNREACH_HOST
                orig_ip = event.parsed.find('ipv4')

                d = orig_ip.pack()
                d = d[:orig_ip.hl * 4 + 8]
                import struct
                d = struct.pack("!HH", 0, 0) + d
                icmp.payload = d
                ipp.payload = icmp
                e.payload = ipp
                msg = of.ofp_packet_out()
                msg.actions.append(of.ofp_action_output(port=event.port))
                msg.data = e.pack()
                self.connection.send(msg)

            return

        log.debug("Installing path for %s -> %s %04x (%i hops)",
            match.dl_src, match.dl_dst, match.dl_type, len(p))

        self._install_path(p, match, event.ofp)

        p = [(sw, out_port, in_port) for sw, in_port, out_port in p]
        self._install_path(p, match.flip())

    def _handle_PacketIn(self, event):
        def flood():
            if self.is_holding_down:
                log.warning("Not flooding -- holddown active")
            msg = of.ofp_packet_out()
            msg.actions.append(of.ofp_action_output(port=of.OFPP_FLOOD))
            msg.buffer_id = event.ofp.buffer_id
            msg.in_port = event.port
            self.connection.send(msg)

        def drop():
            if event.ofp.buffer_id is not None:
                msg = of.ofp_packet_out()
                msg.buffer_id = event.ofp.buffer_id
                event.ofp.buffer_id = None
                msg.in_port = event.port
                self.connection.send(msg)

        packet = event.parsed

        loc = (self, event.port)
        oldloc = mac_map.get(packet.src)

        if packet.effective_ethertype == packet.LLDP_TYPE:
            drop()
            return

        if oldloc is None:
            if packet.src.is_multicast == False:
                mac_map[packet.src] = loc
                log.debug("Learned %s at %s.%i", packet.src, loc[0], loc[1])
        elif oldloc != loc:
            if core.openflow_discovery.is_edge_port(loc[0].dpid, loc[1]):
                log.debug("%s moved from %s.%i to %s.%i?", packet.src,
                          dpid_to_str(oldloc[0].dpid), oldloc[1],
                          dpid_to_str(loc[0].dpid), loc[1])
                if packet.src.is_multicast == False:
                    mac_map[packet.src] = loc
                    log.debug("Learned %s at %s.%i", packet.src, loc[0], loc[1])
            elif packet.dst.is_multicast == False:
                if packet.dst in mac_map:
                    log.warning("Packet from %s to known destination %s arrived "
                                "at %s.%i without flow", packet.src, packet.dst,
                                dpid_to_str(self.dpid), event.port)

        if packet.dst.is_multicast:
            log.debug("Flood multicast from %s", packet.src)
            flood()
        else:
            if packet.dst not in mac_map:
                log.debug("%s unknown -- flooding" % (packet.dst,))
                flood()
            else:
                dest = mac_map[packet.dst]
                match = of.ofp_match.from_packet(packet)
                self.install_path(dest[0], dest[1], match, event)

    def disconnect(self):
        if self.connection is not None:
            log.debug("Disconnect %s" % (self.connection,))
            self.connection.removeListeners(self._listeners)
            self.connection = None
            self._listeners = None

    def connect(self, connection):
        if self.dpid is None:
            self.dpid = connection.dpid
        assert self.dpid == connection.dpid
        if self.ports is None:
            self.ports = connection.features.ports
        self.disconnect()
        log.debug("Connect %s" % (connection,))
        self.connection = connection
        self._listeners = self.listenTo(connection)
        self._connected_at = time.time()

    @property
    def is_holding_down(self):
        if self._connected_at is None:
            return True
        if time.time() - self._connected_at > FLOOD_HOLDDOWN:
            return False
        return True

    def _handle_ConnectionDown(self, event):
        self.disconnect()


class l2_dijkstra (EventMixin):

    _eventMixin_events = set([
        PathInstalled,
    ])

    def __init__(self):
        core.listen_to_dependencies(self, listen_args={'openflow': {'priority': 0}})

    def _handle_openflow_discovery_LinkEvent(self, event):
        def flip(link):
            return Discovery.Link(link[2], link[3], link[0], link[1])

        l = event.link
        sw1 = switches[l.dpid1]
        sw2 = switches[l.dpid2]

        clear = of.ofp_flow_mod(command=of.OFPFC_DELETE)
        for sw in switches.values():
            if sw.connection is None:
                continue
            sw.connection.send(clear)
        path_map.clear()
        _prev_map.clear()

        if event.removed:
            if sw2 in adjacency[sw1]:
                del adjacency[sw1][sw2]
            if sw1 in adjacency[sw2]:
                del adjacency[sw2][sw1]

            for ll in core.openflow_discovery.adjacency:
                if ll == event.link:
                    continue
                if ll.dpid1 == l.dpid1 and ll.dpid2 == l.dpid2:
                    if flip(ll) in core.openflow_discovery.adjacency:
                        adjacency[sw1][sw2] = ll.port1
                        adjacency[sw2][sw1] = ll.port2
                        break
        else:
            if adjacency[sw1][sw2] is None:
                if flip(l) in core.openflow_discovery.adjacency:
                    adjacency[sw1][sw2] = l.port1
                    adjacency[sw2][sw1] = l.port2

            bad_macs = set()
            for mac, (sw, port) in mac_map.items():
                if sw is sw1 and port == l.port1:
                    bad_macs.add(mac)
                if sw is sw2 and port == l.port2:
                    bad_macs.add(mac)
            for mac in bad_macs:
                log.debug("Unlearned %s", mac)
                del mac_map[mac]

    def _handle_openflow_ConnectionUp(self, event):
        sw = switches.get(event.dpid)
        if sw is None:
            sw = Switch()
            switches[event.dpid] = sw
            sw.connect(event.connection)
        else:
            sw.connect(event.connection)

    def _handle_openflow_BarrierIn(self, event):
        wp = waiting_paths.pop((event.dpid, event.xid), None)
        if not wp:
            return
        wp.notify(event)


def launch():
    core.registerNew(l2_dijkstra)

    timeout = min(max(PATH_SETUP_TIME, 5) * 2, 15)
    Timer(timeout, WaitingPath.expire_waiting_paths, recurring=True)