#!/usr/bin/env python3

import pywind.evtframework.excepts as excepts
import os, sys, socket
import pywind.evtframework.handler.handler as handler
import freenet.lib.fn_utils as fn_utils
import pywind.lib.timer as timer

try:
    import fcntl
except ImportError:
    pass


class tun_base(handler.handler):
    __creator_fd = None
    # 要写入到tun的IP包
    ___ip_packets_for_write = []
    # 写入tun设备的最大IP数据包的个数
    __MAX_WRITE_QUEUE_SIZE = 20
    # 当前需要写入tun设备的IP数据包的个数
    __current_write_queue_n = 0

    __BLOCK_SIZE = 16 * 1024

    def __create_tun_dev(self, name):
        """创建tun 设备
        :param name:
        :return fd:
        """
        tun_fd = fn_utils.tuntap_create(name, fn_utils.IFF_TUN | fn_utils.IFF_NO_PI)
        fn_utils.interface_up(name)

        if tun_fd < 0:
            raise SystemError("can not create tun device,please check your root")

        return tun_fd

    @property
    def creator(self):
        return self.__creator_fd

    def init_func(self, creator_fd, tun_dev_name, *args, **kwargs):
        """
        :param creator_fd:
        :param tun_dev_name:tun 设备名称
        :param subnet:如果是服务端则需要则个参数
        """
        tun_fd = self.__create_tun_dev(tun_dev_name)

        if tun_fd < 3:
            print("error:create tun device failed:%s" % tun_dev_name)
            sys.exit(-1)

        self.__creator_fd = creator_fd

        self.set_fileno(tun_fd)
        fcntl.fcntl(tun_fd, fcntl.F_SETFL, os.O_NONBLOCK)
        self.dev_init(tun_dev_name, *args, **kwargs)

        return tun_fd

    def dev_init(self, dev_name, *args, **kwargs):
        pass

    def evt_read(self):
        for i in range(5):
            try:
                ip_packet = os.read(self.fileno, self.__BLOCK_SIZE)
            except BlockingIOError:
                return
            self.handle_ip_packet_from_read(ip_packet)
        return

    def evt_write(self):
        try:
            ip_packet = self.___ip_packets_for_write.pop(0)
        except IndexError:
            self.remove_evt_write(self.fileno)
            return

        self.__current_write_queue_n -= 1
        try:
            os.write(self.fileno, ip_packet)
        except BlockingIOError:
            self.__current_write_queue_n += 1
            self.___ip_packets_for_write.insert(0, ip_packet)
            return
        ''''''

    def handle_ip_packet_from_read(self, ip_packet):
        """处理读取过来的IP包,重写这个方法
        :param ip_packet:
        :return None:
        """
        pass

    def handle_ip_packet_for_write(self, ip_packet):
        """处理要写入的IP包,重写这个方法
        :param ip_packet:
        :return new_ip_packet:
        """
        pass

    def error(self):
        self.dev_error()

    def dev_error(self):
        """重写这个方法
        :return:
        """
        pass

    def timeout(self):
        self.dev_timeout()

    def dev_timeout(self):
        """重写这个方法
        :return:
        """
        pass

    def delete(self):
        self.dev_delete()

    def dev_delete(self):
        """重写这个方法
        :return:
        """
        pass

    def add_to_sent_queue(self, ip_packet):
        # 丢到超出规定的数据包,防止内存过度消耗
        if self.__current_write_queue_n == self.__MAX_WRITE_QUEUE_SIZE:
            return

        self.__current_write_queue_n += 1
        n_ip_message = self.handle_ip_packet_for_write(ip_packet)

        if not n_ip_message:
            return

        self.___ip_packets_for_write.append(n_ip_message)


class tuns(tun_base):
    """服务端的tun数据处理
    """
    __map = None
    __timer = None
    __MAP_TIMEOUT = 600
    __TIMEOUT = 10

    def __add_route(self, dev_name, subnet):
        """给设备添加路由
        :param dev_name:
        :param subnet:
        :return:
        """
        ip, mask_size = subnet
        mask = 0

        for n in range(mask_size):
            mask |= 1 << (31 - n)

        t = socket.inet_aton(ip)
        i_ip = (t[0] << 24) | (t[1] << 16) | (t[2] << 8) | t[3]

        if i_ip & mask != (i_ip):
            print("error:netmask doesn't match route address")
            sys.exit(-1)

        cmd = "route add -net %s/%s dev %s" % (ip, mask_size, dev_name)
        os.system(cmd)

    def dev_init(self, tun_devname, subnet):
        self.register(self.fileno)
        self.add_evt_read(self.fileno)
        self.__add_route(tun_devname, subnet)
        self.__timer = timer.timer()
        self.__map = {}

    def dev_error(self):
        print("error:server tun device error")
        self.delete_handler(self.fileno)

    def handle_ip_packet_from_read(self, ip_packet):
        ip_ver = (ip_packet[0] & 0xf0) >> 4
        if ip_ver != 4: return
        protocol = ip_packet[9]
        if protocol not in (1, 6, 17, 132,): return
        daddr = ip_packet[16:20]
        if daddr not in self.__map: return
        fd = self.__map[daddr]
        try:
            self.send_message_to_handler(self.fileno, fd, ip_packet)
            self.__timer.set_timeout(daddr, self.__MAP_TIMEOUT)
        except excepts.HandlerNotFoundErr:
            return

    def handle_ip_packet_for_write(self, ip_packet):
        return ip_packet

    def dev_delete(self):
        self.unregister(self.fileno)
        os.close(self.fileno)
        sys.exit(-1)

    def message_from_handler(self, from_fd, ip_packet):
        ip_ver = (ip_packet[0] & 0xf0) >> 4
        if ip_ver != 4: return

        saddr = ip_packet[12:16]
        if saddr not in self.__map: self.__map[saddr] = from_fd

        self.__timer.set_timeout(saddr, self.__MAP_TIMEOUT)
        self.add_evt_write(self.fileno)
        self.add_to_sent_queue(ip_packet)

    def dev_timeout(self):
        names = self.__timer.get_timeout_names()
        for name in names:
            if name in self.__map: del self.__map[name]
            if self.__timer.exists(name): self.__timer.drop(name)
        self.set_timeout(self.fileno, self.__TIMEOUT)
