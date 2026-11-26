#!/usr/bin/python
from mininet.net import Mininet
from mininet.node import Controller, OVSKernelSwitch, RemoteController
from mininet.cli import CLI
from mininet.log import setLogLevel

setLogLevel('info')

net = Mininet(controller=RemoteController, switch=OVSKernelSwitch)

h1 = net.addHost('h1')
h2 = net.addHost('h2')
h3 = net.addHost('h3')
h4 = net.addHost('h4')

s1 = net.addSwitch('s1')
s2 = net.addSwitch('s2')
s3 = net.addSwitch('s3')
s4 = net.addSwitch('s4')
s5 = net.addSwitch('s5')

c0 = net.addController('c0', controller=RemoteController, ip="127.0.0.1", port=6633)

net.addLink(h1, s1)
net.addLink(h2, s4)
net.addLink(h3, s5)
net.addLink(h4, s5)

net.addLink(s1, s2)
net.addLink(s1, s5)
net.addLink(s2, s3)
net.addLink(s3, s4)
net.addLink(s3, s5)
net.addLink(s4, s5)

net.start()
CLI(net)
net.stop()