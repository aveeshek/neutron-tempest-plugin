# Copyright 2016 Red Hat, Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
from neutron_lib import constants

from tempest.common import waiters
from tempest.lib.common.utils import data_utils
from tempest.lib import decorators

from neutron_tempest_plugin.common import ssh
from neutron_tempest_plugin import config
from neutron_tempest_plugin.scenario import base
from neutron_tempest_plugin.scenario import constants as const

CONF = config.CONF


class NetworkSecGroupTest(base.BaseTempestTestCase):
    credentials = ['primary', 'admin']
    required_extensions = ['router', 'security-group']

    @classmethod
    def resource_setup(cls):
        super(NetworkSecGroupTest, cls).resource_setup()
        # setup basic topology for servers we can log into it
        cls.network = cls.create_network()
        cls.subnet = cls.create_subnet(cls.network)
        router = cls.create_router_by_client()
        cls.create_router_interface(router['id'], cls.subnet['id'])
        cls.keypair = cls.create_keypair()

    def create_vm_testing_sec_grp(self, num_servers=2, security_groups=None,
                                  ports=None):
        """Create instance for security group testing

        :param num_servers (int): number of servers to spawn
        :param security_groups (list): list of security groups
        :param ports* (list): list of ports
        *Needs to be the same length as num_servers
        """
        servers, fips, server_ssh_clients = ([], [], [])
        for i in range(num_servers):
            server_args = {
                'flavor_ref': CONF.compute.flavor_ref,
                'image_ref': CONF.compute.image_ref,
                'key_name': self.keypair['name'],
                'networks': [{'uuid': self.network['id']}],
                'security_groups': security_groups
            }
            if ports is not None:
                server_args['networks'][0].update({'port': ports[i]['id']})
            servers.append(self.create_server(**server_args))
        for i, server in enumerate(servers):
            waiters.wait_for_server_status(
                self.os_primary.servers_client, server['server']['id'],
                const.SERVER_STATUS_ACTIVE)
            port = self.client.list_ports(
                network_id=self.network['id'], device_id=server['server'][
                    'id'])['ports'][0]
            fips.append(self.create_floatingip(port=port))
            server_ssh_clients.append(ssh.Client(
                fips[i]['floating_ip_address'], CONF.validation.image_ssh_user,
                pkey=self.keypair['private_key']))
        return server_ssh_clients, fips, servers

    def _test_ip_prefix(self, rule_list, should_succeed):
        # Add specific remote prefix to VMs and check connectivity
        ssh_secgrp_name = data_utils.rand_name('ssh_secgrp')
        icmp_secgrp_name = data_utils.rand_name('icmp_secgrp_with_cidr')
        ssh_secgrp = self.os_primary.network_client.create_security_group(
            name=ssh_secgrp_name)
        self.create_loginable_secgroup_rule(
            secgroup_id=ssh_secgrp['security_group']['id'])
        icmp_secgrp = self.os_primary.network_client.create_security_group(
            name=icmp_secgrp_name)
        self.create_secgroup_rules(
            rule_list, secgroup_id=icmp_secgrp['security_group']['id'])
        for sec_grp in (ssh_secgrp, icmp_secgrp):
            self.security_groups.append(sec_grp['security_group'])
        security_groups_list = [{'name': ssh_secgrp_name},
                                {'name': icmp_secgrp_name}]
        server_ssh_clients, fips, servers = self.create_vm_testing_sec_grp(
            security_groups=security_groups_list)

        # make sure ssh connectivity works
        self.check_connectivity(fips[0]['floating_ip_address'],
                                CONF.validation.image_ssh_user,
                                self.keypair['private_key'])

        # make sure ICMP connectivity works
        self.check_remote_connectivity(server_ssh_clients[0], fips[1][
            'fixed_ip_address'], should_succeed=should_succeed)

    @decorators.idempotent_id('3d73ec1a-2ec6-45a9-b0f8-04a283d9d764')
    def test_default_sec_grp_scenarios(self):
        server_ssh_clients, fips, _ = self.create_vm_testing_sec_grp()
        # Check ssh connectivity when you add sec group rule, enabling ssh
        self.create_loginable_secgroup_rule(
            self.os_primary.network_client.list_security_groups()[
                'security_groups'][0]['id']
        )
        self.check_connectivity(fips[0]['floating_ip_address'],
                                CONF.validation.image_ssh_user,
                                self.keypair['private_key'])

        # make sure ICMP connectivity still fails as only ssh rule was added
        self.ping_ip_address(fips[0]['floating_ip_address'],
                             should_succeed=False)

        # Check ICMP connectivity between VMs without specific rule for that
        # It should work though the rule is not configured
        self.check_remote_connectivity(
            server_ssh_clients[0], fips[1]['fixed_ip_address'])

        # Check ICMP connectivity from VM to external network
        subnets = self.os_admin.network_client.list_subnets(
            network_id=CONF.network.public_network_id)['subnets']
        ext_net_ip = None
        for subnet in subnets:
            if subnet['ip_version'] == 4:
                ext_net_ip = subnet['gateway_ip']
                break
        self.assertTrue(ext_net_ip)
        self.check_remote_connectivity(server_ssh_clients[0], ext_net_ip)

    @decorators.idempotent_id('3d73ec1a-2ec6-45a9-b0f8-04a283d9d864')
    def test_protocol_number_rule(self):
        # protocol number is added instead of str in security rule creation
        server_ssh_clients, fips, _ = self.create_vm_testing_sec_grp(
            num_servers=1)
        self.ping_ip_address(fips[0]['floating_ip_address'],
                             should_succeed=False)
        rule_list = [{'protocol': constants.PROTO_NUM_ICMP,
                      'direction': constants.INGRESS_DIRECTION,
                      'remote_ip_prefix': '0.0.0.0/0'}]
        secgroup_id = self.os_primary.network_client.list_security_groups()[
            'security_groups'][0]['id']
        self.create_secgroup_rules(rule_list, secgroup_id=secgroup_id)
        self.ping_ip_address(fips[0]['floating_ip_address'])

    @decorators.idempotent_id('3d73ec1a-2ec6-45a9-b0f8-04a283d9d964')
    def test_two_sec_groups(self):
        # add 2 sec groups to VM and test rules of both are working
        ssh_secgrp_name = data_utils.rand_name('ssh_secgrp')
        icmp_secgrp_name = data_utils.rand_name('icmp_secgrp')
        ssh_secgrp = self.os_primary.network_client.create_security_group(
            name=ssh_secgrp_name)
        self.create_loginable_secgroup_rule(
            secgroup_id=ssh_secgrp['security_group']['id'])
        icmp_secgrp = self.os_primary.network_client.create_security_group(
            name=icmp_secgrp_name)
        self.create_pingable_secgroup_rule(
            secgroup_id=icmp_secgrp['security_group']['id'])
        for sec_grp in (ssh_secgrp, icmp_secgrp):
            self.security_groups.append(sec_grp['security_group'])
        security_groups_list = [{'name': ssh_secgrp_name},
                                {'name': icmp_secgrp_name}]
        server_ssh_clients, fips, servers = self.create_vm_testing_sec_grp(
            num_servers=1, security_groups=security_groups_list)
        # make sure ssh connectivity works
        self.check_connectivity(fips[0]['floating_ip_address'],
                                CONF.validation.image_ssh_user,
                                self.keypair['private_key'])
        # make sure ICMP connectivity works
        self.ping_ip_address(fips[0]['floating_ip_address'],
                             should_succeed=True)
        ports = self.client.list_ports(device_id=servers[0]['server']['id'])
        port_id = ports['ports'][0]['id']

        # update port with ssh security group only
        self.os_primary.network_client.update_port(
            port_id, security_groups=[ssh_secgrp['security_group']['id']])

        # make sure ssh connectivity works
        self.check_connectivity(fips[0]['floating_ip_address'],
                                CONF.validation.image_ssh_user,
                                self.keypair['private_key'])

        # make sure ICMP connectivity doesn't work
        self.ping_ip_address(fips[0]['floating_ip_address'],
                             should_succeed=False)

        # update port with ssh and ICMP security groups
        self.os_primary.network_client.update_port(
            port_id, security_groups=[
                icmp_secgrp['security_group']['id'],
                ssh_secgrp['security_group']['id']])

        # make sure ssh connectivity  works after update
        self.check_connectivity(fips[0]['floating_ip_address'],
                                CONF.validation.image_ssh_user,
                                self.keypair['private_key'])

        # make sure ICMP connectivity works after update
        self.ping_ip_address(fips[0]['floating_ip_address'])

    @decorators.idempotent_id('3d73ec1a-2ec6-45a9-b0f8-04a283d9d664')
    def test_ip_prefix(self):
        cidr = self.subnet['cidr']
        rule_list = [{'protocol': constants.PROTO_NUM_ICMP,
                      'direction': constants.INGRESS_DIRECTION,
                      'remote_ip_prefix': cidr}]
        self._test_ip_prefix(rule_list, should_succeed=True)

    @decorators.attr(type='negative')
    @decorators.idempotent_id('a01cd2ef-3cfc-4614-8aac-9d1333ea21dd')
    def test_ip_prefix_negative(self):
        # define bad CIDR
        cidr = '10.100.0.254/32'
        rule_list = [{'protocol': constants.PROTO_NUM_ICMP,
                      'direction': constants.INGRESS_DIRECTION,
                      'remote_ip_prefix': cidr}]
        self._test_ip_prefix(rule_list, should_succeed=False)

    @decorators.idempotent_id('7ed39b86-006d-40fb-887a-ae46693dabc9')
    def test_remote_group(self):
        # create a new sec group
        ssh_secgrp_name = data_utils.rand_name('ssh_secgrp')
        ssh_secgrp = self.os_primary.network_client.create_security_group(
            name=ssh_secgrp_name)
        # add cleanup
        self.security_groups.append(ssh_secgrp['security_group'])
        # configure sec group to support SSH connectivity
        self.create_loginable_secgroup_rule(
            secgroup_id=ssh_secgrp['security_group']['id'])
        # spawn two instances with the sec group created
        server_ssh_clients, fips, servers = self.create_vm_testing_sec_grp(
            security_groups=[{'name': ssh_secgrp_name}])
        # verify SSH functionality
        for i in range(2):
            self.check_connectivity(fips[i]['floating_ip_address'],
                                    CONF.validation.image_ssh_user,
                                    self.keypair['private_key'])
        # try to ping instances without ICMP permissions
        self.check_remote_connectivity(
            server_ssh_clients[0], fips[1]['fixed_ip_address'],
            should_succeed=False)
        # add ICMP support to the remote group
        rule_list = [{'protocol': constants.PROTO_NUM_ICMP,
                      'direction': constants.INGRESS_DIRECTION,
                      'remote_group_id': ssh_secgrp['security_group']['id']}]
        self.create_secgroup_rules(
            rule_list, secgroup_id=ssh_secgrp['security_group']['id'])
        # verify ICMP connectivity between instances works
        self.check_remote_connectivity(
            server_ssh_clients[0], fips[1]['fixed_ip_address'])
        # make sure ICMP connectivity doesn't work from framework
        self.ping_ip_address(fips[0]['floating_ip_address'],
                             should_succeed=False)

    @decorators.idempotent_id('f07d0159-8f9e-4faa-87f5-a869ab0ad488')
    def test_multiple_ports_secgroup_inheritance(self):
        """Test multiple port security group inheritance

        This test creates two ports with security groups, then
        boots two instances and verify that the security group was
        inherited properly and enforced in these instances.
        """
        # create a security group and make it loginable and pingable
        secgrp = self.os_primary.network_client.create_security_group(
            name=data_utils.rand_name('secgrp'))
        self.create_loginable_secgroup_rule(
            secgroup_id=secgrp['security_group']['id'])
        self.create_pingable_secgroup_rule(
            secgroup_id=secgrp['security_group']['id'])
        # add security group to cleanup
        self.security_groups.append(secgrp['security_group'])
        # create two ports with fixed IPs and the security group created
        ports = []
        for i in range(2):
            ports.append(self.create_port(
                self.network, fixed_ips=[{'subnet_id': self.subnets[0]['id']}],
                security_groups=[secgrp['security_group']['id']]))
        # spawn instances with the ports created
        server_ssh_clients, fips, servers = self.create_vm_testing_sec_grp(
            ports=ports)
        # verify ICMP reachability and ssh connectivity
        for fip in fips:
            self.ping_ip_address(fip['floating_ip_address'])
            self.check_connectivity(fip['floating_ip_address'],
                                    CONF.validation.image_ssh_user,
                                    self.keypair['private_key'])
