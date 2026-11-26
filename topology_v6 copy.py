#!/usr/bin/python
from mininet.net import Mininet
from mininet.node import Controller, OVSKernelSwitch, RemoteController
from mininet.cli import CLI
from mininet.log import setLogLevel

setLogLevel('info')

net = Mininet(controller=RemoteController, switch=OVSKernelSwitch)

# Хосты
h1 = net.addHost('h1')
h2 = net.addHost('h2')
h3 = net.addHost('h3')
h4 = net.addHost('h4')

# Коммутаторы
s1 = net.addSwitch('s1', failMode='secure')
s2 = net.addSwitch('s2', failMode='secure')
s3 = net.addSwitch('s3', failMode='secure')
s4 = net.addSwitch('s4', failMode='secure')
s5 = net.addSwitch('s5', failMode='secure')

# Контроллер POX
c0 = net.addController('c0', controller=RemoteController,
                       ip="127.0.0.1", port=6633)

# --- Связи ---
# Хосты
net.addLink(h1, s1)
net.addLink(h2, s4)
net.addLink(h3, s5)
net.addLink(h4, s5)

# Коммутаторы (избыточные связи)
net.addLink(s1, s2)
net.addLink(s2, s3)
net.addLink(s3, s4)
net.addLink(s5, s1)
net.addLink(s5, s3)
net.addLink(s5, s4)

net.start()

# --- Настройка STP (вариант 6): s2 — ROOT ---
s2.cmd('ovs-vsctl set Bridge s2 other_config:stp-priority=0x1000')  # ROOT
s1.cmd('ovs-vsctl set Bridge s1 other_config:stp-priority=0x7000')
s3.cmd('ovs-vsctl set Bridge s3 other_config:stp-priority=0x8000')
s4.cmd('ovs-vsctl set Bridge s4 other_config:stp-priority=0x9000')
s5.cmd('ovs-vsctl set Bridge s5 other_config:stp-priority=0xA000')

s1.cmd('ovs-vsctl set bridge s1 stp_enable=true')
s2.cmd('ovs-vsctl set bridge s2 stp_enable=true')
s3.cmd('ovs-vsctl set bridge s3 stp_enable=true')
s4.cmd('ovs-vsctl set bridge s4 stp_enable=true')
s5.cmd('ovs-vsctl set bridge s5 stp_enable=true')

# Проверка состояния портов STP
s1.cmdPrint('ovs-ofctl dump-ports-desc s1')
s2.cmdPrint('ovs-ofctl dump-ports-desc s2')
s3.cmdPrint('ovs-ofctl dump-ports-desc s3')
s4.cmdPrint('ovs-ofctl dump-ports-desc s4')
s5.cmdPrint('ovs-ofctl dump-ports-desc s5')

CLI(net)
net.stop()