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

# Waiting path.  (dpid,xid)->WaitingPath
waiting_paths = {}

FLOOD_HOLDDOWN = 5
FLOW_IDLE_TIMEOUT = 10
FLOW_HARD_TIMEOUT = 30
PATH_SETUP_TIME = 4

def _calc_paths():
    path_map.clear()
    graph = {}
    
    for sw_dpid in adjacency:
        graph[sw_dpid] = {}
        for port, neighbor_dpid in adjacency[sw_dpid].items():
            if neighbor_dpid is not None:
                graph[sw_dpid][neighbor_dpid] = 1

    for src in graph:
        dist = {node: float('inf') for node in graph}
        dist[src] = 0
        prev = {node: None for node in graph}
        pq = [(0, src)]

        while pq:
            current_dist, u = heapq.heappop(pq)
            if current_dist > dist.get(u, float('inf')):
                continue
                
            for v, weight in graph.get(u, {}).items():
                distance = current_dist + weight
                if distance < dist.get(v, float('inf')):
                    dist[v] = distance
                    prev[v] = u
                    heapq.heappush(pq, (distance, v))

        for dst in graph:
            if src == dst:
                path_map[src][dst] = (0, None)
            elif dist.get(dst) != float('inf'):
                curr = dst
                while prev.get(curr) != src and prev.get(curr) is not None:
                    curr = prev[curr]
                first_hop = curr
                path_map[src][dst] = (dist[dst], first_hop)

def _get_raw_path (src, dst):
  if len(path_map) == 0: _calc_paths()
  if src is dst:
    return []
  if path_map[src][dst][0] is None:
    return None
  intermediate = path_map[src][dst][1]
  if intermediate is None:
    return []
  return _get_raw_path(src, intermediate) + [intermediate] + _get_raw_path(intermediate, dst)

def _check_path (p):
  for a,b in zip(p[:-1],p[1:]):
    if adjacency[a[0]][b[0]] != a[2]:
      return False
    if adjacency[b[0]][a[0]] != b[1]:
      return False
  return True

def _get_path (src, dst, first_port, final_port):
  if src == dst:
    path = [src]
  else:
    path = _get_raw_path(src, dst)
    if path is None: return None
    path = [src] + path + [dst]

  r = []
  in_port = first_port
  for s1,s2 in zip(path[:-1],path[1:]):
    out_port = adjacency[s1][s2]
    r.append((s1,in_port,out_port))
    in_port = adjacency[s2][s1]
  r.append((dst,in_port,final_port))
  return r


class WaitingPath (object):
  def __init__ (self, path, packet):
    self.expires_at = time.time() + PATH_SETUP_TIME
    self.path = path
    self.first_switch = path[0][0].dpid
    self.xids = set()
    self.packet = packet

    if len(waiting_paths) > 1000:
      WaitingPath.expire_waiting_paths()

  def add_xid (self, dpid, xid):
    self.xids.add((dpid,xid))
    waiting_paths[(dpid,xid)] = self

  @property
  def is_expired (self):
    return time.time() >= self.expires_at

  def notify (self, event):
    self.xids.discard((event.dpid,event.xid))
    if len(self.xids) == 0:
      if self.packet:
        log.debug("Sending delayed packet out %s" % (dpid_to_str(self.first_switch),))
        msg = of.ofp_packet_out(data=self.packet,
            action=of.ofp_action_output(port=of.OFPP_TABLE))
        core.openflow.sendToDPID(self.first_switch, msg)
      core.l2_multi.raiseEvent(PathInstalled(self.path))

  @staticmethod
  def expire_waiting_paths ():
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
  def __init__ (self, path):
    self.path = path

class Switch (EventMixin):
  def __init__ (self):
    self.connection = None
    self.ports = None
    self.dpid = None
    self._listeners = None
    self._connected_at = None

  def __repr__ (self):
    return dpid_to_str(self.dpid)

  def _install (self, switch, in_port, out_port, match, buf = None):
    msg = of.ofp_flow_mod()
    msg.match = match
    msg.match.in_port = in_port
    msg.idle_timeout = FLOW_IDLE_TIMEOUT
    msg.hard_timeout = FLOW_HARD_TIMEOUT
    msg.actions.append(of.ofp_action_output(port = out_port))
    msg.buffer_id = buf
    switch.connection.send(msg)

  def _install_path (self, p, match, packet_in=None):
    wp = WaitingPath(p, packet_in)
    for sw,in_port,out_port in p:
      self._install(sw, in_port, out_port, match)
      msg = of.ofp_barrier_request()
      sw.connection.send(msg)
      wp.add_xid(sw.dpid,msg.xid)

  def install_path (self, dst_sw, last_port, match, event):
    p = _get_path(self, dst_sw, event.port, last_port)
    if p is None:
      log.warning("Can't get from %s to %s", match.dl_src, match.dl_dst)
      return

    log.debug("Installing path for %s -> %s %04x (%i hops)",
        match.dl_src, match.dl_dst, match.dl_type, len(p))

    self._install_path(p, match, event.ofp)
    p = [(sw,out_port,in_port) for sw,in_port,out_port in p]
    self._install_path(p, match.flip())

  def _handle_PacketIn (self, event):
    def flood ():
      if self.is_holding_down: return
      msg = of.ofp_packet_out()
      msg.actions.append(of.ofp_action_output(port = of.OFPP_FLOOD))
      msg.buffer_id = event.ofp.buffer_id
      msg.in_port = event.port
      self.connection.send(msg)

    def drop ():
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
        if packet.src.is_multicast == False:
          mac_map[packet.src] = loc
          log.debug("Learned %s at %s.%i", packet.src, loc[0], loc[1])

    if packet.dst.is_multicast:
      flood()
    else:
      if packet.dst not in mac_map:
        flood()
      else:
        dest = mac_map[packet.dst]
        match = of.ofp_match.from_packet(packet)
        self.install_path(dest[0], dest[1], match, event)

  def disconnect (self):
    if self.connection is not None:
      self.connection.removeListeners(self._listeners)
      self.connection = None
      self._listeners = None

  def connect (self, connection):
    if self.dpid is None: self.dpid = connection.dpid
    if self.ports is None: self.ports = connection.features.ports
    self.disconnect()
    self.connection = connection
    self._listeners = self.listenTo(connection)
    self._connected_at = time.time()

  @property
  def is_holding_down (self):
    if self._connected_at is None: return True
    if time.time() - self._connected_at > FLOOD_HOLDDOWN:
      return False
    return True

  def _handle_ConnectionDown (self, event):
    self.disconnect()

class l2_multi (EventMixin):
  _eventMixin_events = set([PathInstalled])

  def __init__ (self):
    core.listen_to_dependencies(self, listen_args={'openflow':{'priority':0}})

  def _handle_openflow_discovery_LinkEvent (self, event):
    def flip (link):
      return Discovery.Link(link[2],link[3], link[0],link[1])

    l = event.link
    sw1 = switches.get(l.dpid1)
    sw2 = switches.get(l.dpid2)

    if not sw1 or not sw2:
        return

    clear = of.ofp_flow_mod(command=of.OFPFC_DELETE)
    for sw in switches.values():
      if sw.connection is None: continue
      sw.connection.send(clear)
    path_map.clear()

    if event.removed:
      if sw2 in adjacency[sw1]: del adjacency[sw1][sw2]
      if sw1 in adjacency[sw2]: del adjacency[sw2][sw1]
    else:
      if adjacency[sw1][sw2] is None:
        if flip(l) in core.openflow_discovery.adjacency:
          adjacency[sw1][sw2] = l.port1
          adjacency[sw2][sw1] = l.port2

      bad_macs = set()
      for mac,(sw,port) in mac_map.items():
        if sw is sw1 and port == l.port1: bad_macs.add(mac)
        if sw is sw2 and port == l.port2: bad_macs.add(mac)
      for mac in bad_macs:
        del mac_map[mac]

  def _handle_openflow_ConnectionUp (self, event):
    sw = switches.get(event.dpid)
    if sw is None:
      sw = Switch()
      switches[event.dpid] = sw
      sw.connect(event.connection)
    else:
      sw.connect(event.connection)

  def _handle_openflow_BarrierIn (self, event):
    wp = waiting_paths.pop((event.dpid,event.xid), None)
    if wp: wp.notify(event)

def launch ():
  core.registerNew(l2_multi)
  timeout = min(max(PATH_SETUP_TIME, 5) * 2, 15)
  Timer(timeout, WaitingPath.expire_waiting_paths, recurring=True)