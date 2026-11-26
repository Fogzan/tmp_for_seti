#!/usr/bin/python
from mininet.net import Mininet
from mininet.node import Controller, OVSKernelSwitch, RemoteController
from mininet.cli import CLI
from mininet.log import setLogLevel

setLogLevel('info')

# Используем удаленный контроллер (POX)
net = Mininet(controller=RemoteController, switch=OVSKernelSwitch)

# 1. Создаем хосты
h1 = net.addHost('h1')
h2 = net.addHost('h2')
h3 = net.addHost('h3')
h4 = net.addHost('h4')

# 2. Создаем коммутаторы (failMode='secure' обязателен для работы с POX)
s1 = net.addSwitch('s1', failMode='secure')
s2 = net.addSwitch('s2', failMode='secure')
s3 = net.addSwitch('s3', failMode='secure')
s4 = net.addSwitch('s4', failMode='secure')
s5 = net.addSwitch('s5', failMode='secure')

# 3. Подключаем контроллер
c0 = net.addController('c0', controller=RemoteController, ip="127.0.0.1", port=6633)

# 4. Соединяем хосты с коммутаторами
net.addLink(h1, s1)
net.addLink(h2, s4)
net.addLink(h3, s5)
net.addLink(h4, s5)

# 5. Соединяем коммутаторы между собой (создаем петли)
net.addLink(s1, s2)
net.addLink(s2, s3)
net.addLink(s3, s4)
net.addLink(s1, s5)
net.addLink(s3, s5)
net.addLink(s4, s5)

# Запуск
net.start()
CLI(net)
net.stop()