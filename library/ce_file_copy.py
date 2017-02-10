#!/usr/bin/python
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.
#

ANSIBLE_METADATA = {'status': ['preview'],
                    'supported_by': 'community',
                    'version': '1.0'}

DOCUMENTATION = '''
---
module: ce_file_copy
version_added: "2.3"
short_description: Copy a file to a remote cloudengine device over SCP.
description:
    - Copy a file to a remote cloudengine device over SCP.
extends_documentation_fragment: cloudengine
author:
    - Zhou Zhijin (@CloudEngine-Ansible)
notes:
    - The feature must be enabled with feature scp-server.
    - If the file is already present, no transfer will take place.
options:
    local_file:
        description:
            - Path to local file. Local directory must exist.
              The maximum length of local_file is 4096.
        required: true
    remote_file:
        description:
            - Remote file path of the copy. Remote directories must exist.
              If omitted, the name of the local file will be used.
              The maximum length of remote_file is 4096.
        required: false
        default: null
    file_system:
        description:
            - The remote file system of the device. If omitted,
              devices that support a file_system parameter will use
              their default values.
              File system indicates the storage medium and can be set to as follows,
              1) 'flash:' is root directory of the flash memory on the master MPU.
              2) 'slave#flash:' is root directory of the flash memory on the slave MPU.
                 If no slave MPU exists, this drive is unavailable.
              3) 'chassis ID/slot number#flash:' is root directory of the flash memory on
                 a device in a stack. For example, 1/5#flash indicates the flash memory
                 whose chassis ID is 1 and slot number is 5.
        required: false
        default: 'flash:'
'''

EXAMPLES = '''
#Copy a local file to remote device
- ce_file_copy: local_file=/usr/vrpcfg.cfg remote_file=/vrpcfg.cfg file_system=flash:
'''

RETURN = '''
changed:
    description: check to see if a change was made on the device
    returned: always
    type: boolean
    sample: true
transfer_result:
    description: information about transfer result.
    returned: always
    type: string
    sample: 'The local file has been successfully transferred to the device.'
local_file:
    description: The path of the local file.
    returned: always
    type: string
    sample: '/usr/work/vrpcfg.zip'
remote_file:
    description: The path of the remote file.
    returned: always
    type: string
    sample: '/vrpcfg.zip'
'''

import sys
import re
import os
import time
from xml.etree import ElementTree
import paramiko
from ansible.module_utils.shell import ShellError
from ansible.module_utils.basic import get_exception
from ansible.module_utils.network import NetworkModule, NetworkError
from ansible.module_utils.netcli import FailedConditionsError, FailedConditionalError
from ansible.module_utils.netcli import AddCommandError
from ansible.module_utils.cloudengine import get_netconf
from ansible.module_utils.netcli import CommandRunner
from ansible.module_utils.cloudengine import get_cli_exception

try:
    from ncclient.operations.rpc import RPCError
    HAS_NCCLIENT = True
except ImportError:
    HAS_NCCLIENT = False

try:
    from scp import SCPClient
    HAS_SCP = True
except ImportError:
    HAS_SCP = False

CE_NC_GET_FILE_INFO = """
<filter type="subtree">
  <vfm xmlns="http://www.huawei.com/netconf/vrp" content-version="1.0" format-version="1.0">
    <dirs>
      <dir>
        <fileName>%s</fileName>
        <dirName>%s</dirName>
        <DirSize></DirSize>
      </dir>
    </dirs>
  </vfm>
</filter>
"""

CE_NC_GET_SCP_ENABLE = """
<filter type="subtree">
  <sshs xmlns="http://www.huawei.com/netconf/vrp" content-version="1.0" format-version="1.0">
    <sshServer>
      <scpEnable></scpEnable>
    </sshServer>
  </sshs>
</filter>
"""


class FileCopy(object):
    """file copy function class"""

    def __init__(self, argument_spec):
        self.spec = argument_spec
        self.module = None
        self.netconf = None
        self.init_module()

        # file copy parameters
        self.local_file = self.module.params['local_file']
        self.remote_file = self.module.params['remote_file']
        self.file_system = self.module.params['file_system']

        # host info
        self.host = self.module.params['host']
        self.username = self.module.params['username']
        self.port = self.module.params['port']

        # state
        self.transfer_result = None
        self.changed = False

        # init netconf connect
        self.init_netconf()

    def init_module(self):
        """ init_module"""

        self.module = NetworkModule(
            argument_spec=self.spec, supports_check_mode=True)

    def init_netconf(self):
        """ init_netconf"""

        if HAS_NCCLIENT:
            self.netconf = get_netconf(host=self.host, port=self.port,
                                       username=self.username,
                                       password=self.module.params['password'])
        else:
            self.module.fail_json(
                msg='Error: No ncclient package, please install it.')

    def check_response(self, con_obj, xml_name):
        """Check if response message is already succeed."""

        xml_str = con_obj.xml
        if "<ok/>" not in xml_str:
            self.module.fail_json(msg='Error: %s failed.' % xml_name)

    def netconf_set_config(self, xml_str, xml_name):
        """ netconf set config """

        try:
            con_obj = self.netconf.set_config(config=xml_str)
            self.check_response(con_obj, xml_name)
        except RPCError:
            err = sys.exc_info()[1]
            self.module.fail_json(msg='Error: %s' %
                                  err.message.replace('\r\n', ''))

        return con_obj

    def netconf_get_config(self, xml_str):
        """ netconf get config """

        try:
            con_obj = self.netconf.get_config(filter=xml_str)
        except RPCError:
            err = sys.exc_info()[1]
            self.module.fail_json(msg='Error: %s' %
                                  err.message.replace('\r\n', ''))

        return con_obj

    def remote_file_exists(self, dst, file_system='flash:'):
        """ remote file whether exists """

        full_path = file_system + dst
        file_name = os.path.basename(full_path)
        file_path = os.path.dirname(full_path)
        file_path = file_path + '/'
        xml_str = CE_NC_GET_FILE_INFO % (file_name, file_path)
        con_obj = self.netconf_get_config(xml_str)
        if "<data/>" in con_obj.xml:
            return False, 0

        xml_str = con_obj.xml.replace('\r', '').replace('\n', '').\
            replace('xmlns="urn:ietf:params:xml:ns:netconf:base:1.0"', "").\
            replace('xmlns="http://www.huawei.com/netconf/vrp"', "")

        # get file info
        root = ElementTree.fromstring(xml_str)
        topo = root.find("data/vfm/dirs/dir")
        if topo is None:
            return False, 0

        for eles in topo:
            if eles.tag in ["DirSize"]:
                return True, int(eles.text.replace(',', ''))

        return False, 0

    def local_file_exists(self):
        """ local file whether exists """

        return os.path.isfile(self.local_file)

    def excute_command(self, commands):
        """ excute_command"""

        output = ''
        runner = CommandRunner(self.module)
        for cmd in commands:
            try:
                runner.add_command(**cmd)
            except AddCommandError:
                self.module.fail_json(
                    msg='duplicate command detected: %s' % cmd)

        try:
            runner.run()
        except FailedConditionsError:
            exc = get_exception()
            self.module.fail_json(
                msg=get_cli_exception(exc), failed_conditions=exc.failed_conditions)
        except FailedConditionalError:
            exc = get_exception()
            self.module.fail_json(
                msg=get_cli_exception(exc), failed_conditional=exc.failed_conditional)
        except NetworkError:
            exc = get_exception()
            self.module.fail_json(msg=get_cli_exception(exc))

        for cmd in commands:
            try:
                output = runner.get_command(cmd['command'], cmd.get('output'))
            except ValueError:
                self.module.fail_json(
                    msg='command not executed due to check_mode, see warnings')
        return output

    def enough_space(self):
        """ whether device has enough space"""

        commands = list()
        cmd = {'output': None, 'command': 'dir %s' % self.file_system}
        commands.append(cmd)
        output = self.excute_command(commands)
        if not output:
            return True

        match = re.search(r'\((.*) KB free\)', output)
        kbytes_free = match.group(1)
        kbytes_free = kbytes_free.replace(',', '')

        file_size = os.path.getsize(self.local_file)
        if int(kbytes_free) * 1024 > file_size:
            return True

        return False

    def transfer_file(self, dest):
        """ begin to transfer file by scp"""

        if not self.local_file_exists():
            self.module.fail_json(
                msg='Could not transfer file. Local file doesn\'t exist.')

        if not self.enough_space():
            self.module.fail_json(
                msg='Could not transfer file. Not enough space on device.')

        hostname = self.module.params['host']
        username = self.module.params['username']
        password = self.module.params['password']
        port = self.module.params['port']

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(hostname=hostname, username=username,
                    password=password, port=port)

        full_remote_path = '{}{}'.format(self.file_system, dest)
        scp = SCPClient(ssh.get_transport())
        try:
            scp.put(self.local_file, full_remote_path)
        except:
            time.sleep(10)
            file_exists, temp_size = self.remote_file_exists(
                dest, self.file_system)
            file_size = os.path.getsize(self.local_file)
            if file_exists and int(temp_size) == int(file_size):
                pass
            else:
                scp.close()
                self.module.fail_json(msg='Could not transfer file. There was an error '
                                      'during transfer. Please make sure the format of '
                                      'input parameters is right.')

        scp.close()
        return True

    def get_scp_enable(self):
        """ get scp enable state"""

        xml_str = CE_NC_GET_SCP_ENABLE
        con_obj = self.netconf_get_config(xml_str)
        if "<data/>" in con_obj.xml:
            return False

        xml_str = con_obj.xml.replace('\r', '').replace('\n', '').\
            replace('xmlns="urn:ietf:params:xml:ns:netconf:base:1.0"', "").\
            replace('xmlns="http://www.huawei.com/netconf/vrp"', "")

        # get file info
        root = ElementTree.fromstring(xml_str)
        topo = root.find("data/sshs/sshServer")
        if topo is None:
            return False

        for eles in topo:
            if eles.tag in ["scpEnable"]:
                return True, eles.text

        return False

    def work(self):
        """ excute task """

        if not HAS_SCP:
            self.module.fail_json(
                msg="'Error: No scp package, please install it.'")

        if self.local_file and len(self.local_file) > 4096:
            self.module.fail_json(
                msg="'Error: The maximum length of local_file is 4096.'")

        if self.remote_file and len(self.remote_file) > 4096:
            self.module.fail_json(
                msg="'Error: The maximum length of remote_file is 4096.'")

        retcode, cur_state = self.get_scp_enable()
        if retcode and cur_state == 'Disable':
            self.module.fail_json(
                msg="'Error: Please ensure SCP server is enabled.'")

        if not os.path.isfile(self.local_file):
            self.module.fail_json(
                msg="Local file {} not found".format(self.local_file))

        dest = self.remote_file or ('/' + os.path.basename(self.local_file))
        remote_exists, file_size = self.remote_file_exists(
            dest, file_system=self.file_system)
        if remote_exists and (os.path.getsize(self.local_file) != file_size):
            remote_exists = False

        if not remote_exists:
            self.changed = True
            file_exists = False
        else:
            file_exists = True
            self.transfer_result = 'The local file already exists on the device.'

        if not file_exists:
            try:
                self.transfer_file(dest)
                self.transfer_result = 'The local file has been successfully ' \
                                       'transferred to the device.'
            except ShellError:
                clie = get_exception()
                self.module.fail_json(msg=get_cli_exception(clie))

        if self.remote_file is None:
            self.remote_file = '/' + os.path.basename(self.local_file)

        self.module.exit_json(
            changed=self.changed,
            transfer_result=self.transfer_result,
            local_file=self.local_file,
            remote_file=self.remote_file,
            file_system=self.file_system)


def main():
    """ main function entry"""
    argument_spec = dict(
        local_file=dict(required=True),
        remote_file=dict(required=False),
        file_system=dict(required=False, default='flash:')
    )

    filecopy_obj = FileCopy(argument_spec)
    filecopy_obj.work()

if __name__ == '__main__':
    main()
