#!/usr/bin/env python3
r"""

Simple end-to-end test of FAUCET config agent

We create a simple network using Mininet,
start up FAUCET, and then use the config agent
to install various configurations, which we
then test to make sure that they are behaving
as expected.

Test setup (host:vlan)

  h1:100 - s1 - s2 - h3:100
  h2:200 /         \ h4:200

We verify that only [h1,h2] and [h2,h4]
can ping each other, while other distinct host pairs
cannot.

Then we renumber the VLANs and switch the groups:

  h1:300 - s1 - s2 - h3:400
  h2:400 /         \ h4:300

Now we verify that [h1,h4] and [h2,h3] can ping
each other, while the other distinct host pairs
cannot.

We may wish to add some tests of various failure
modes, such as FAUCET dying, the agent dying,
or losing connectivity between the agent and FAUCET.

"""

from subprocess import run, Popen, PIPE
from time import sleep, time

from mininet.net import Mininet
from mininet.node import Controller
from mininet.topo import Topo
from mininet.util import irange
from mininet.log import setLogLevel, info, error
from mininet.util import decode

# pylint: disable=too-few-public-methods


class TestTopo(Topo):
    r"""
     Simple test topology:
        h1 - s1 - s2 - h3
        h2 /         \ h4
    """

    def build(self, *args, **kwargs):
        "Build test topology"
        del args, kwargs
        # pylint: disable=invalid-name
        s1, s2 = [self.addSwitch('s%d' % i) for i in irange(1, 2)]
        h1, h2, h3, h4 = [self.addHost('h%d' % i) for i in irange(1, 4)]
        for host in h1, h2:
            self.addLink(s1, host)
        for host in h3, h4:
            self.addLink(s2, host)
        self.addLink(s1, s2)


# FAUCET configuration template
#
# Maybe we should generate the config automatically
# rather than just substituting into a template

CONFIG = """
vlans:
  office:
    vid: {vid1}
  guest:
    vid: {vid2}

dps:
  s1:
    dp_id: 0x1
    hardware: "Open vSwitch"
    interfaces:
      1:
        name: "h1"
        native_vlan: {h1_vlan}
      2:
        name: "h2"
        native_vlan: {h2_vlan}
      3:
        name: "link"
        tagged_vlans: [office, guest]
  s2:
    dp_id: 0x2
    hardware: "Open vSwitch"
    interfaces:
      1:
        name: "h3"
        native_vlan: {h3_vlan}
      2:
        name: "h4"
        native_vlan: {h4_vlan}
      3:
        name: "link"
        tagged_vlans: [office, guest]
"""

TEST_CASES = (
    # First Test case:
    # [h1,h3] in office vlan, [h2,h4] in guest vlan
    dict(
        vid1=100,
        vid2=200,
        h1_vlan='office',
        h2_vlan='guest',
        h3_vlan='office',
        h4_vlan='guest',
        groups=(['h1', 'h3'], ['h2', 'h4'])),
    # Second Test case:
    # [h1,h4] in guest vlan, [h2,h3] in office vlan
    dict(
        vid1=300,
        vid2=400,
        h1_vlan='guest',
        h2_vlan='office',
        h3_vlan='office',
        h4_vlan='guest',
        groups=(['h1', 'h4'], ['h2', 'h3'])))


def check(hosts, groups):
    """Check VLAN connectivity groups, returning error count"""

    vlan = {host: group for group in groups for host in group}

    # Start pings
    pings = [(src, dst, src.popen('ping -c1 -w1 %s' % dst.IP()))
             for src in hosts for dst in hosts]

    errors = 0

    # Collect and verify ping results
    for src, dst, ping in pings:

        out, err = ping.communicate()
        result = decode(out + err)
        ping.wait()

        # The space before '0%' is very important
        dropped = '100% packet loss' in result
        sent = ' 0% packet loss' in result

        # Sanity check
        if sent == dropped:
            raise RuntimeError('ping failed with output: %s' % result)

        info(src, '->', dst, 'sent' if sent else 'dropped', '\n')

        # Ping should only succeed when src and dst are in the same VLAN
        connected = (vlan[src] == vlan[dst])
        if sent != connected:
            error('ERROR:', src, 'should'
                  if connected else 'should not', 'be able to ping', dst, '\n')
            errors += 1

    # Return error count
    return errors


#
# FAUCET Controller class
#


class FAUCET(Controller):
    """Simple FAUCET controller class"""

    cfile = 'faucet.yaml'
    timeout = 20

    def start(self):
        """Start FAUCET"""
        env = ('FAUCET_CONFIG=' + self.cfile, 'FAUCET_LOG=STDOUT',
               'FAUCET_EXCEPTION_LOG=STDERR')
        self.cmd('export', *env)
        self.cmd('faucet 1>faucet.log 2>&1 &')
        if not wait_server(port=9302, timeout=self.timeout):
            error('Timeout waiting for FAUCET to start. Log:\n')
            with open('faucet.log') as log:
                error(log.read())

    def stop(self, *args, **kwargs):
        """Stop FAUCET"""
        del args, kwargs
        self.cmd('kill %faucet')
        self.cmd('wait')


# Certificate management:
#
# gnxi requires TLS, but testing it is a pain since we need to
# deal with certificates, signing authorities, etc.
#
# I'm not entirely sure I'm doing this correctly, but we
# create a fake CA and use it to sign a fake server cert.
# Then we generate a self-signed client certificate and use
# it to connect.

GNMI_PORT = 10161  # Agent listening port (default: gNMI port 10161)
CERT_DIR = 'testcerts'  # We create and destroy this dir to store test certs
TARGET = 'localhost'  # hostname use in certs and passed to -target
SUBJ = '/CN=' + TARGET  # Minimal specification for a cert


def make_certs():
    """Create fake certificates for agent and client"""

    def do(*cmds):  # pylint: disable=invalid-name
        """Run a bunch of commands via subprocess.run()"""
        for cmd in cmds:
            run(cmd.format(cert_dir=CERT_DIR, subj=SUBJ).split(),
                stdout=PIPE,
                stderr=PIPE,
                check=True)

    do('rm -rf {cert_dir}', 'mkdir {cert_dir}')

    info('* Generating fake CA cert\n')

    do('openssl req -x509 -sha256 -nodes -days 2 -newkey rsa:2048'
       ' -keyout {cert_dir}/fakeca.key -out {cert_dir}/fakeca.crt '
       ' -subj {subj}')

    info('* Generating and signing fake server cert\n')

    do('openssl genrsa -out {cert_dir}/fakeserver.key 2048',
       'openssl req -new -key {cert_dir}/fakeserver.key'
       ' -out {cert_dir}/fakeserver.csr -subj {subj}',
       'openssl x509 -req -days 2 -in {cert_dir}/fakeserver.csr'
       ' -CA {cert_dir}/fakeca.crt -CAkey {cert_dir}/fakeca.key'
       ' -set_serial 01 -out {cert_dir}/fakeserver.crt')

    info('* Generating fake client cert (self-signed)\n')

    do('openssl req -x509 -sha256 -nodes -days 2 -newkey rsa:2048'
       ' -keyout {cert_dir}/fakeclient.key -out {cert_dir}/fakeclient.crt'
       ' -subj {subj}')


# Utility routines


def write_file(filename, data):
    """(Over)write a file with data"""
    with open(filename, 'w') as ref:
        ref.write(data)


def wait_server(port, timeout=20):
    """Wait for server to listen on port"""
    cmd, start = ['fuser', '%d/tcp' % port], time()
    while True:
        if run(cmd, stdout=PIPE).returncode == 0:
            return True
        if time() - start > timeout:
            break
        sleep(1)
    return False


def kill_server(port, timeout=20):
    """Shut down server listening on port"""
    cmd, start = ['fuser', '-k', '-9', '%d/tcp' % port], time()
    while True:
        if run(cmd, stdout=PIPE).returncode != 0:
            return True
        if time() - start > timeout:
            break
        sleep(1)
    return False


def unescape(string):
    """Un-escape a string"""
    return string.encode('utf8').decode('unicode_escape')


def string_val(output):
    """Extract first string_val:... from output"""
    lines = output.split('\n')
    strings = [line for line in lines if 'string_val:' in line]
    if not strings:
        return ''
    line = unescape(strings[0]).split('string_val:')[1].strip()
    return line


#
# End-to-end agent test
#

# pylint: disable=too-many-locals


def end_to_end_test():
    """Simple end-to-end test of FAUCET config agent"""

    # Start with empty FAUCET config file
    write_file(FAUCET.cfile, '')

    info('* Generating certificates\n')
    make_certs()
    params = dict(cert_dir=CERT_DIR, gnmi_port=GNMI_PORT, cfile=FAUCET.cfile)
    client_auth = (' -ca {cert_dir}/fakeca.crt -cert {cert_dir}/fakeclient.crt'
                   ' -key {cert_dir}/fakeclient.key'
                   ' -target_name localhost').format(**params).split()

    info('* Starting network\n')
    net = Mininet(topo=TestTopo(), controller=FAUCET)
    net.start()

    info('* Shutting down any agents listening on %d\n' % GNMI_PORT)
    kill_server(port=GNMI_PORT)

    info('* Starting agent\n')
    agent_log = open('faucetagent.log', 'w')
    agent_cmd = ('./faucetagent.py  --cert {cert_dir}/fakeserver.crt'
                 ' --key {cert_dir}/fakeserver.key'
                 ' --gnmiport {gnmi_port}'
                 ' --configfile {cfile}'
                 ' --dpwait 1.0').format(**params).split()
    agent = Popen(agent_cmd, stdout=agent_log, stderr=agent_log)

    info('* Waiting for agent to start up\n')
    wait_server(port=GNMI_PORT)

    fail_count = 0

    for test_num, test_case in enumerate(TEST_CASES):

        # Get the test case configuration
        config = CONFIG.format(**test_case)

        info('* Sending test configuration to agent\n')
        # Pylint doesn't understand [x, *y, z] apparently
        cmd = ['gnmi_set'] + client_auth + ['-replace=/:' + config]
        result = run(cmd, stdout=PIPE, check=True)
        sent = string_val(result.stdout.decode())

        info('* Fetching configuration from agent\n')
        cmd = ['gnmi_get'] + client_auth + ['-xpath=/']
        result = run(cmd, stdout=PIPE, check=True)

        received = string_val(result.stdout.decode())

        info('* Verifying received configuration\n')
        if sent != received:
            error('ERROR: received config differs from sent config\n')

        # FAUCET currently sets 'applied' after it has configured all
        # switches, but doesn't currently wait for a barrier reply
        # (and it doesn't use barriers with OVS anyway.)
        # For now we are stuck waiting a bit for the messages to
        # take effect.
        # pylint: disable=fixme
        # TODO: check OVS flow table state
        info('* Waiting two seconds for switch eventual consistency\n')
        sleep(2)

        groups = test_case['groups']
        info('* Verifying connectivity for', groups, '\n')
        host_groups = [net.get(*group) for group in groups]
        errors = check(hosts=net.hosts, groups=host_groups)
        info('Test Case #%d:' % test_num, 'OK'
             if errors == 0 else 'FAIL (%d errors)' % errors, '\n')
        if errors:
            fail_count += 1

    info('* Stopping agent\n')
    agent.terminate()
    agent.wait()
    agent_log.close()

    info('* Stopping network\n')
    net.stop()
    return fail_count


def main():
    "Entry point as per google pyguide"
    setLogLevel('info')
    exit_code = end_to_end_test()
    exit(exit_code)


if __name__ == '__main__':
    main()
