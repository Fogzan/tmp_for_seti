#!/usr/bin/python
from mininet.net import Mininet
from mininet.node import OVSKernelSwitch
from mininet.cli import CLI
from mininet.log import setLogLevel

setLogLevel('info')
net = Mininet(switch=OVSKernelSwitch)

h1=net.addHost('h1'); h2=net.addHost('h2'); h3=net.addHost('h3'); h4=net.addHost('h4')
s1=net.addSwitch('s1'); s2=net.addSwitch('s2'); s3=net.addSwitch('s3')
s4=net.addSwitch('s4'); s5=net.addSwitch('s5')

net.addLink(h1,s1); net.addLink(h2,s4); net.addLink(h3,s5); net.addLink(h4,s5)
net.addLink(s1,s2); net.addLink(s2,s3); net.addLink(s3,s4)
net.addLink(s1,s5); net.addLink(s3,s5); net.addLink(s4,s5)

net.start()

# приоритеты: s2 — корень
s2.cmd('ovs-vsctl set Bridge s2 other_config:stp-priority=0x1000')
s3.cmd('ovs-vsctl set Bridge s3 other_config:stp-priority=0x2000')
s1.cmd('ovs-vsctl set Bridge s1 other_config:stp-priority=0x3000')
s4.cmd('ovs-vsctl set Bridge s4 other_config:stp-priority=0x4000')
s5.cmd('ovs-vsctl set Bridge s5 other_config:stp-priority=0x5000')

# стоимости портов (имена eth соответствуют порядку addLink выше)
COST = {
 's1-eth2':1,'s2-eth1':1,   # s1-s2
 's2-eth2':1,'s3-eth1':1,   # s2-s3
 's3-eth2':4,'s4-eth2':4,   # s3-s4  (тяжёлый)
 's1-eth3':4,'s5-eth3':4,   # s1-s5  (тяжёлый)
 's3-eth3':1,'s5-eth4':1,   # s3-s5
 's4-eth3':1,'s5-eth5':1,   # s4-s5
}
for iface,c in COST.items():
    sw, port = iface.split('-')
    net.get(sw).cmd('ovs-vsctl set Interface %s other_config:stp-path-cost=%d' % (iface,c))

for s in (s1,s2,s3,s4,s5):
    s.cmd('ovs-vsctl set bridge %s stp_enable=true' % s.name)

CLI(net)
net.stop()