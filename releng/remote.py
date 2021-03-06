#!/usr/bin/env python

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

""" releng

    :copyright: (c) 2011 by Mozilla
    :license: MPLv2

    Assumes Python v2.6+

    Authors:
        catlee   Chris Atlee <catlee@mozilla.com>
        coop     Chris Cooper <coop@mozilla.com>
        bear     Mike Taylor <bear@mozilla.com>
        jhopkins John Hopkins <jhopkins@mozilla.com>
"""

import os
import re
import time
import json
import socket
import logging
from datetime import datetime
from pytz import timezone
import telnetlib
import ssh
import requests
import dns.resolver

from multiprocessing import get_logger
from . import fetchUrl, runCommand, getPassword, getSecrets, relative
from releng.buildapi import last_build_endtime

log = get_logger()

urlSlaveAlloc = 'http://slavealloc.build.mozilla.org/api'


class Host(object):
    prompt = "$ "
    bbdir  = "/builds/slave"

    def __init__(self, hostname, remoteEnv, verbose=False):
        self.verbose   = verbose
        self.remoteEnv = remoteEnv
        self.hostname  = hostname
        self.farm      = None
        self.fqdn      = None
        self.ip        = None
        self.isTegra   = False
        self.hasPDU    = False
        self.hasIPMI   = False
        self.IPMIip    = None
        self.IPMIhost  = None
        self.channel   = None
        self.foopy     = None
        self.client    = None
        self.info      = None
        self.pinged    = False
        self.reachable = False
        self.pdu = {
            'pdu': None,
            'deviceID': None,
        }

        logging.getLogger("ssh.transport").setLevel(logging.WARNING)

        if 'ec2' in hostname:
            if hostname in remoteEnv.hosts:
                self.info = remoteEnv.hosts[hostname]
                self.ip   = self.info['ip']
                self.fqdn = self.ip
        else:
            if '.' in hostname:
                fullhostname = hostname
                hostname     = hostname.split('.', 1)[0]
            else:
                fullhostname = '%s.build.mozilla.org' % hostname

            if hostname in remoteEnv.hosts:
                self.info = remoteEnv.hosts[hostname]

            try:
                dnsAnswer = dns.resolver.query(fullhostname)
                self.fqdn = '%s' % dnsAnswer.canonical_name
                self.ip   = dnsAnswer[0]
            except:
                log.error('exception raised during fqdn lookup for [%s]' % fullhostname, exc_info=True)
                self.fqdn = None

            if self.fqdn is not None:
                try:
                    self.IPMIhost = "%s-mgmt.build.mozilla.org" % (hostname)
                    dnsAnswer     = dns.resolver.query(self.IPMIhost)
                    self.IPMIip   = dnsAnswer[0]
                    self.hasIPMI  = True
                except:
                    self.IPMIhost = None
                    self.IPMIip   = None

        if hostname.startswith('tegra'):
            self.isTegra = True
            self.farm    = 'tegra'
            self.bbdir   = '/builds/%s' % hostname
        else:
            if 'ec2' in hostname:
                self.farm = 'ec2'
            else:
                self.farm = 'moz'

        if self.fqdn is not None and not remoteEnv.passive:
            if self.farm == 'ec2':
                self.pinged = self.info['state'] == 'running'
            else:
                self.pinged, output = self.ping()
            if self.pinged or self.isTegra:
                if verbose:
                    log.info('creating SSHClient')
                self.client = ssh.SSHClient()
                self.client.set_missing_host_key_policy(ssh.AutoAddPolicy())
            else:
                if verbose:
                    log.info('unable to ping %s' % hostname)

            if self.isTegra:
                self.bbdir = '/builds/%s' % hostname

                if hostname in remoteEnv.tegras:
                    self.foopy = remoteEnv.tegras[hostname]['foopy']
                    log.info('foopy: %s' % self.foopy)

                try:
                    self.tegra = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

                    self.tegra.settimeout(float(120))
                    self.tegra.connect((self.fqdn, 20700))
                    self.reachable = True
                except:
                    log.error('socket error establishing connection to tegra data port', exc_info=True)
                    self.tegra = None

                if self.foopy is not None:
                    try:
                        self.client.connect('%s.build.mtv1.mozilla.com' % self.foopy, username=remoteEnv.sshuser, password=remoteEnv.sshPassword, allow_agent=False, look_for_keys=False)
                        self.transport = self.client.get_transport()
                        self.channel   = self.transport.open_session()
                        self.channel.get_pty()
                        self.channel.invoke_shell()
                    except:
                        log.error('socket error establishing ssh connection', exc_info=True)
                        self.client = None
            else:
                if self.pinged:
                    try:
                        if self.verbose:
                            log.info('connecting to remote host')
                        self.client.connect(self.fqdn, username=remoteEnv.sshuser, password=remoteEnv.sshPassword, allow_agent=False, look_for_keys=False)
                        self.transport = self.client.get_transport()
                        if self.verbose:
                            log.info('opening session')
                        self.channel   = self.transport.open_session()
                        self.channel.get_pty()
                        if self.verbose:
                            log.info('invoking remote shell')
                        self.channel.invoke_shell()
                        self.reachable = True
                    except:
                        log.error('socket error establishing ssh connection', exc_info=True)
                        self.client = None
        if self.setPDUFromInventory():
            self.hasPDU = True

    def graceful_shutdown(self, indent='', dryrun=False):
        if not self.buildbot_active():
            return False

        tacinfo = self.get_tacinfo()

        if tacinfo is None:
            log.error("%sCouldn't get info from buildbot.tac; host is disabled?" % indent)
            return False

        host, port, hostname = tacinfo

        if 'staging' in host:
            log.warn("%sIgnoring staging host %s for host %s" % (indent, host, self.hostname))
            return False

        # HTTP port is host port - 1000
        port -= 1000

        # Look at the host's page
        url = "http://%s:%i/buildslaves/%s" % (host, port, hostname)
        if self.verbose:
            log.info("%sFetching host page %s" % (indent, url))
        data = fetchUrl('%s?numbuilds=0' % url)

        if data is None:
            return False

        if "Graceful Shutdown" not in data:
            log.error("%sno shutdown form for %s" % (indent, self.hostname))
            return False

        if self.verbose:
            log.info("%sSetting shutdown" % indent)
        if dryrun:
            log.info("%sShutdown deferred" % indent)
        else:
            data = fetchUrl("%s/shutdown" % url)
            if data is None:
                return False

        return True

    def buildbot_active(self):
        cmd  = 'ls -l %s/twistd.pid' % self.bbdir
        data = self.run_cmd(cmd)
        m    = re.search('No such file or directory$', data)
        if m:
            return False
        cmd  = 'ps ww `cat %s/twistd.pid`' % self.bbdir
        data = self.run_cmd(cmd)
        m    = re.search('buildbot', data)
        if m:
            return True
        return False

    def cat_buildbot_tac(self):
        cmd = "cat %s/buildbot.tac" % self.bbdir
        return self.run_cmd(cmd)

    def get_tacinfo(self):
        log.debug("Determining host's master")
        data   = self.cat_buildbot_tac()
        master = re.search('^buildmaster_host\s*=\s*["\'](.*)["\']', data, re.M)
        port   = re.search('^port\s*=\s*(\d+)', data, re.M)
        host   = re.search('^slavename\s*=\s*["\'](.*)["\']', data, re.M)
        if master and port and host:
            return master.group(1), int(port.group(1)), host.group(1)

    def run_cmd(self, cmd, fetch_output=True):
        log.debug("Running %s", cmd)
        if self.client is None:
            data = ''
        else:
            try:
                self.channel.sendall("%s\r\n" % cmd)
            except: # socket.error:
                log.error('socket error', exc_info=True)
                return
            data = None
            if fetch_output:
                data = self.wait()
                log.debug(data)
        return data

    def _read(self):
        buf = []
        while self.channel.recv_ready():
            data = self.channel.recv(1024)
            if not data:
                break
            buf.append(data)
        buf = "".join(buf)

        # Strip out ANSI escape sequences
        # Setting position
        buf = re.sub('\x1b\[\d+;\d+f', '', buf)
        buf = re.sub('\x1b\[\d+m', '', buf)
        return buf

    def wait(self):
        log.debug('waiting for remote shell to respond')
        buf = []
        n = 0
        if self.client is not None:
            while True:
                try:
                    self.channel.sendall("\r\n")
                    data = self._read()
                    buf.append(data)
                    if data.endswith(self.prompt) and not self.channel.recv_ready():
                        break
                    time.sleep(1)
                    n += 1
                    if n > 15:
                        log.error('timeout waiting for shell')
                        break
                except: # socket.error:
                    log.error('exception during wait()', exc_info=True)
                    self.client = None
                    break
        return "".join(buf)

    def ping(self):
        # bash-3.2$ ping -c 2 -o tegra-056
        # PING tegra-056.build.mtv1.mozilla.com (10.250.49.43): 56 data bytes
        # 64 bytes from 10.250.49.43: icmp_seq=0 ttl=64 time=1.119 ms
        #
        # --- tegra-056.build.mtv1.mozilla.com ping statistics ---
        # 1 packets transmitted, 1 packets received, 0.0% packet loss
        # round-trip min/avg/max/stddev = 1.119/1.119/1.119/0.000 ms
        out    = []
        result = False

        p, o = runCommand(['ping', '-c 5', self.fqdn], logEcho=False)
        for s in o:
            out.append(s)
            if '5 packets transmitted, 5 packets received' in s or '5 packets transmitted, 5 received' in s:
                result = True
                break
        return result, out

    def setPDUFromInventory(self):
        remoteEnv = self.remoteEnv
        if None in [remoteEnv.inventoryURL, remoteEnv.inventoryUsername, remoteEnv.inventoryPassword]:
            log.info("No inventory configuration found; skipping PDU reboot")
            return False
        _fqdn = self.fqdn
        if _fqdn is None:
            log.info("FQDN not set, skipping inventory fetch")
            return False
        if _fqdn.endswith('.'):
            _fqdn = _fqdn[:-1]
        url = '%s/en-US/tasty/v3/system/?hostname=%s' % (remoteEnv.inventoryURL, _fqdn)
        user = remoteEnv.inventoryUsername
        password = remoteEnv.inventoryPassword
        log.debug('Fetching %s' % url)
        r = requests.get(url, auth=(user, password))
        if r.status_code == 200:
            pdu = ""
            deviceID = ""
            rjson = r.json()
            if rjson['meta']['total_count'] == 0:
                log.info("No inventory record found for '%s', cannot look up PDU details" % _fqdn)
            else:
                for key_value in rjson['objects'][0]['key_value']:
                    if key_value['key'] == 'system.pdu.0':
                        (pdu, deviceID) = key_value['value'].split(':')
                        if not pdu.endswith('.mozilla.com'):
                            pdu = pdu + '.mozilla.com'
                        break
                if pdu == '' or deviceID == '':
                    log.debug("Could not locate system.pdu.0 in inventory data")
                else:
                    log.debug('Fetched PDU details from inventory')
                    self.pdu['pdu'] = pdu
                    self.pdu['deviceID'] = deviceID
                    return True
        return False

    def logRebootAttempt(self, rebootMethod, result, message):
        logFile = "/home/buildduty/briar-patch/logs/slave_reboots/%s.json" % self.hostname
        logMessage = {
            'asctime': datetime.now(),
            'reboot':  rebootMethod,
            'result':  result,
            'message': message,
            }
        dthandler = lambda obj: obj.isoformat() if isinstance(obj, datetime) else None

        rebootAttempts = []
        if os.path.exists(logFile) and os.path.getsize(logFile) > 0:
            json_data = open(logFile)
            rebootAttempts = json.load(json_data)

        rebootAttempts.insert(0, logMessage)

        with open(logFile, 'w') as rebootLog:
            rebootLog.write(json.dumps(rebootAttempts, sort_keys=True, indent=4, default=dthandler))
            rebootLog.write("\n")

    def rebootPDU(self):
        result = False

        if None in [self.pdu['pdu'], self.pdu['deviceID']]:
            log.warn('No pdu or deviceID available in rebootPDU')
            return result

        pdu = self.pdu['pdu']
        deviceID = self.pdu['deviceID']
        log.debug("pdu='%s', deviceID='%s'" % (pdu, deviceID))

        if deviceID[1] == 'B':
            b = 2
        else:
            b = 1

        log.debug('rebooting %s at %s %s' % (self.hostname, pdu, deviceID))
        c   = int(deviceID[2:])
        s   = '3.2.3.1.11.1.%d.%d' % (b, c)
        oib = '1.3.6.1.4.1.1718.%s' % s
        cmd = '/usr/bin/snmpset -v 1 -c private %s %s i 3' % (pdu, oib)

        try:
            log.info('Running: %s' % cmd)
            result = os.system(cmd) == 0
        except:
            log.error('error running [%s]' % cmd, exc_info=True)
            result = False
        self.logRebootAttempt('PDU', result, cmd)
        return result

    # code by Catlee, bugs by bear
    def rebootIPMI(self, timeout=10):
        result = False
        if self.hasIPMI:
            log.debug('logging into ipmi for %s at %s' % (self.hostname, self.IPMIip))
            url = "http://%s/cgi/login.cgi" % self.IPMIip
            try:
                r = requests.post(url, data={ 'name': self.remoteEnv.ipmiUser,
                                              'pwd':  self.remoteEnv.ipmiPassword,
                                              },
                                  timeout=timeout)
                
                if r.status_code == 200:
                    # Push the button!
                    # e.g.
                    # http://10.12.48.105/cgi/ipmi.cgi?POWER_INFO.XML=(1%2C3)&time_stamp=Wed%20Mar%2021%202012%2010%3A26%3A57%20GMT-0400%20(EDT)
                    url = "http://%s/cgi/ipmi.cgi" % self.IPMIip
                    log.debug("logged in. sending power cycle request via %s" % url)
                    r = requests.get(url,
                                     params={ 'POWER_INFO.XML': "(1,3)",
                                              'time_stamp': time.strftime("%a %b %d %Y %H:%M:%S"),
                                            },
                                     cookies = r.cookies,
                                     timeout=timeout
                                    )
                else:
                    log.error('error during rebootIPMI request [%s] [%s]' % (url, r.status_code))

                result = r.status_code == 200
            except:
                log.error('error connecting to IPMI', exc_info=True)
                result = False
            self.logRebootAttempt('IPMI', result, url)
        else:
            log.debug('IPMI not available')

        return result

class UnixishHost(Host):
    def find_buildbot_tacfiles(self):
        cmd = "ls -l %s/buildbot.tac*" % self.bbdir
        data = self.run_cmd(cmd)
        tacs = []
        exp = "\d+ %s/(buildbot\.tac(?:\.\w+)?)" % self.bbdir
        for m in re.finditer(exp, data):
            tacs.append(m.group(1))
        return tacs

    def tail_twistd_log(self, n=100):
        cmd = "tail -%i %s/twistd.log" % (n, self.bbdir)
        return self.run_cmd(cmd)

    def reboot(self):
        # assume that if we have a working command channel,
        # the reboot will succeed
        rv = self.run_cmd("echo test")
        if ('test' in rv):
            cmd = "sudo reboot"
            self.run_cmd(cmd, fetch_output=False)
            self.logRebootAttempt('ssh', True, cmd)
            return True
        else:
            return False

class LinuxBuildHost(UnixishHost):
    prompt = "]$ "
    bbdir  = "/builds/slave"

class LinuxIXTalosHost(UnixishHost):
    prompt = "$ "
    bbdir  = "/builds/slave/talos-slave"

class LinuxTalosHost(UnixishHost):
    prompt = "]$ "
    bbdir  = "/home/cltbld/talos-slave"

class OSXBuildHost(UnixishHost):
    prompt = "cltbld$ "
    bbdir  = "/builds/slave"

class OSXPDUHost(UnixishHost):
    prompt = "cltbld$ "
    bbdir  = "/builds/slave"

class OSXTalosHost(OSXPDUHost):
    prompt = "cltbld$ "
    bbdir  = "/Users/cltbld/talos-slave"

class WinHost(Host):
    msysdir = ''

    def _read(self):
        buf = []
        if self.client is not None:
            while self.channel.recv_ready():
                data = self.channel.recv(1024)
                if not data:
                    break
                buf.append(data)
            buf = "".join(buf)

            # Strip out ANSI escape sequences
            # Setting position
            buf = re.sub('\x1b\[\d+;\d+f', '', buf)
        return buf

    def wait(self):
        buf = []
        n   = 0
        if self.client is not None:
            while True:
                try:
                    self.channel.sendall("\r\n")
                    data = self._read()
                    buf.append(data)
                    if data.endswith(">") and not self.channel.recv_ready():
                        break
                    time.sleep(1)
                    n += 1
                    if n > 15:
                        log.error('timeout waiting for shell')
                        break
                except: # socket.error:
                    log.error('socket error', exc_info=True)
                    self.client = None
                    break
        return "".join(buf)

    def buildbot_active(self):
        # for now just return True as it was assuming that it was active before
        return True

    def find_buildbot_tacfiles(self):
        cmd = "dir %s\\buildbot.tac*" % self.bbdir
        data = self.run_cmd(cmd)
        tacs = []
        for m in re.finditer("\d+ (buildbot\.tac(?:\.\w+)?)", data):
            tacs.append(m.group(1))
        return tacs

    def cat_buildbot_tac(self):
        cmd = "%scat.exe %s\\buildbot.tac" % (self.msysdir, self.bbdir)
        return self.run_cmd(cmd)

    def tail_twistd_log(self, n=100):
        cmd = "%stail.exe -%i %s\\twistd.log" % (self.msysdir, n, self.bbdir)
        return self.run_cmd(cmd)

    def reboot(self):
        cmd = "shutdown -f -r -t 0"
        self.logRebootAttempt('ssh', True, cmd)
        return self.run_cmd(cmd)

class Win32BuildHost(WinHost):
    bbdir   = "E:\\builds\\moz2_slave"
    msysdir = 'D:\\mozilla-build\\msys\\bin\\'

class Win32TalosHost(WinHost):
    bbdir   = "C:\\talos-slave"
    msysdir = ''

class Win64BuildHost(WinHost):
    bbdir   = "E:\\builds\\moz2_slave"
    msysdir = ''

class Win64TalosHost(WinHost):
    bbdir   = "C:\\talos-slave"
    msysdir = ''

class Win864TalosHost(WinHost):
    bbdir   = "C:\\slave"
    msysdir = ''

class Win732TalosHost(WinHost):
    bbdir   = "C:\\slave"
    msysdir = ''

class WinXP32TalosHost(WinHost):
    bbdir   = "C:\\slave"
    msysdir = ''

class TegraHost(UnixishHost):
    prompt = "cltbld$ "

    def reboot(self):
        self.checkErrorFlag()
        return self.rebootPDU()

    def formatSDCard(self):
        log.info('formatting SDCard')
        tn = telnetlib.Telnet(self.fqdn, 20701)
        log.debug('telnet: %s' % tn.read_until('$>'))
        tn.write('exec newfs_msdos -F 32 /dev/block/vold/179:9\n')
        out = tn.read_until('$>')

        log.debug('telnet: %s' % out)
        if 'return code [0]' in out:
            log.info('SDCard formatted, rebooting Tegra')
            tn.write('exec rebt\n')
            return True
        else:
            log.error('SDCard format failed')
            return False

    def checkErrorFlag(self):
        log.debug("Checking the error flag")
        cmd = "cat %s/error.flg" % self.bbdir
        data = self.run_cmd(cmd)
        result = False
        if re.search('Unable to properly remove /mnt/sdcard/tests', data, re.M):
            result = self.formatSDCard()
        if result:
            return self.removeErrorFlag()
        else:
            return result

    def removeErrorFlag(self):
        log.debug("Removing the error flag")
        cmd = "rm -f %s/error.flg" % self.bbdir
        return self.run_cmd(cmd)

    def rebootPDU(self):
        """
        Try to reboot the given host, returning True if successful.

        snmpset -c private pdu4.build.mozilla.org 1.3.6.1.4.1.1718.3.2.3.1.11.1.1.13 i 3
        1.3.6.1.4.1.1718.3.2.3.1.11.a.b.c
                                    ^^^^^ outlet id
                                 ^^       control action
                               ^          outlet entry
                             ^            outlet tables
                           ^              system tables
                         ^                sentry
        ^^^^^^^^^^^^^^^^                  serverTech enterprises
        a   Sentry enclosure ID: 1 master 2 expansion
        b   Input Power Feed: 1 infeed-A 2 infeed-B
        c   Outlet ID (1 - 16)
        y   command: 1 turn on, 2 turn off, 3 reboot

        a and b are determined by the DeviceID we get from the devices.json file

           .AB14
              ^^ Outlet ID
             ^   InFeed code
            ^    Enclosure ID (we are assuming 1 (or A) below)
        """
        result = False
        cmd = ""
        if self.hostname in self.remoteEnv.tegras:
            pdu      = self.remoteEnv.tegras[self.hostname]['pdu']
            deviceID = self.remoteEnv.tegras[self.hostname]['pduid']
            if deviceID.startswith('.'):
                if deviceID[2] == 'B':
                    b = 2
                else:
                    b = 1

                log.debug('rebooting %s at %s %s' % (self.hostname, pdu, deviceID))
                c   = int(deviceID[3:])
                s   = '3.2.3.1.11.1.%d.%d' % (b, c)
                oib = '1.3.6.1.4.1.1718.%s' % s
                cmd = '/usr/bin/snmpset -v 1 -c private %s %s i 3' % (pdu, oib)

                try:
                    log.info('Running: %s' % cmd)
                    result = os.system(cmd) == 0
                except:
                    log.error('error running [%s]' % cmd, exc_info=True)
                    result = False
        else:
            log.info("Cannot PDU reboot tegra: No match for '%s' in '%s'" % (self.hostname, self.remoteEnv.tegras))

        self.logRebootAttempt('PDU', result, cmd)
        return result

class AWSHost(UnixishHost):
    prompt = "]$ "
    bbdir  = "/builds/slave"

    def wait(self):
        log.debug('waiting for remote shell to respond')
        buf = []
        n   = 0
        if self.client is not None:
            while True:
                try:
                    data = self._read()
                    buf.append(data)
                    if data.endswith(self.prompt) and not self.channel.recv_ready():
                        break
                    time.sleep(0.3)
                    n += 1
                    if n > 30:
                        log.error('timeout waiting for shell')
                        break
                except: # socket.error:
                    log.error('exception during wait()', exc_info=True)
                    self.client = None
                    break
        return "".join(buf)



def msg(msg, indent='', verbose=False):
    if verbose:
        log.info('%s%s' % (indent, msg))
    return msg

def getLogTimeDelta(line):
    td = None
    try:
        ts = datetime.strptime(line[:19], '%Y-%m-%d %H:%M:%S')
        td = datetime.now() - ts
    except:
        td = None
    return td

class RemoteEnvironment():
    def __init__(self, toolspath, sshuser='cltbld', ldapUser=None, ipmiUser='releng', db=None, passive=False):
        self.toolspath = toolspath
        self.sshuser   = sshuser
        self.ldapUser  = ldapUser
        self.ipmiUser  = ipmiUser
        self.db        = db
        self.passive   = passive
        self.tegras    = {}
        self.hosts     = {}
        self.masters   = {}
        self.inventoryURL = None
        self.inventoryUsername = None
        self.inventoryPassword = None

        if self.sshuser is not None:
            self.sshPassword = getPassword(self.sshuser)

        if self.ldapUser is not None:
            self.ldapPassword = getPassword(self.ldapUser)

        if self.ipmiUser is not None:
            self.ipmiPassword = getPassword(self.ipmiUser)

        if getSecrets('inventory') is not None:
            inventory_config = getSecrets('inventory')
            self.inventoryURL = inventory_config['url']
            self.inventoryUsername = inventory_config['username']
            self.inventoryPassword = inventory_config['password']

        if not self.loadTegras(os.path.join(self.toolspath, 'buildfarm/mobile')):
            self.loadTegras('.')

        self.getHostInfo()

    def findMaster(self, masterName):
        if masterName is not None:
            for m in self.masters:
                master = self.masters[m]
                if master is not None and ((master['nickname'] == masterName) or (masterName in master['fqdn'])):
                    return master
        return None

    def getHostInfo(self):
        self.hosts = {}
        # grab and process slavealloc list into a simple dictionary
        j = fetchUrl('%s/slaves' % urlSlaveAlloc)
        if j is None:
            hostlist = []
        else:
            hostlist = json.loads(j)

        self.masters = {}
        j = fetchUrl('%s/masters' % urlSlaveAlloc)
        if j is not None:
            m = json.loads(j)
            for item in m:
                self.masters[item['nickname']] = item

        environments = {}
        j = fetchUrl('%s/environments' % urlSlaveAlloc)
        if j is not None:
            e = json.loads(j)
            for item in e:
                environments[item['envid']] = item['name']

        for item in hostlist:
            if item['envid'] in environments:
                item['environment'] = environments[item['envid']]
            if item['notes'] is None:
                item['notes'] = ''
            self.hosts[item['name']] = item

        if self.db is not None:
            for item in self.db.smembers('farm:ec2'):
                if 'ec2-' in item:
                    instance = self.db.hgetall(item)
                    if instance is not None and 'name' in instance:
                        hostname = instance['name']
                        self.hosts[hostname] = { 'name':           hostname,
                                                 'enabled':        False,
                                                 'environment':    'prod',
                                                 'purpose':        'build',
                                                 'datacenter':     'aws',
                                                 'current_master': None,
                                                 'notes':          '',
                                                 }
                        for key in ('farm', 'moz-state', 'image_id', 'id', 'ipPrivate', 'region', 'state', 'launchTime'):
                            self.hosts[hostname][key] = instance[key]

                        self.hosts[hostname]['class']   = '%s-ec2' % instance['moz-type']
                        self.hosts[hostname]['enabled'] = instance['moz-state'] == 'ready'
                        self.hosts[hostname]['ip']      = instance['ipPrivate']

    def getHost(self, hostname, verbose=False):
        if 'w32-ix' in hostname or 'mw32-ix' in hostname or \
           'moz2-win32' in hostname or 'try-w32-' in hostname or \
           'win32-' in hostname:
            result = Win32BuildHost(hostname, self, verbose=verbose)

        elif 'w64-ix' in hostname:
            result = Win64BuildHost(hostname, self, verbose=verbose)

        elif 'talos-r3-fed' in hostname:
            result = LinuxTalosHost(hostname, self, verbose=verbose)

        elif 'talos-r3-snow' in hostname or 'talos-r4' in hostname or \
             'talos-r3-leopard' in hostname:
            result = OSXTalosHost(hostname, self, verbose=verbose)

        elif 'talos-mtnlion-r5-' in hostname:
            result = OSXTalosHost(hostname, self, verbose=verbose)
            result.bbdir = '/builds/slave/talos-slave'

        elif 'talos-r3-xp' in hostname or 'w764' in hostname or \
             'talos-r3-w7' in hostname:
            result = Win32TalosHost(hostname, self, verbose=verbose)

        elif 't-xp32-ix-' in hostname:
            result = WinXP32TalosHost(hostname, self, verbose=verbose)

        elif 't-w864' in hostname:
            result = Win864TalosHost(hostname, self, verbose=verbose)

        elif 't-w732-ix' in hostname:
            result = Win732TalosHost(hostname, self, verbose=verbose)

        elif 'talos-linux32-ix' in hostname or 'talos-linux64-ix' in hostname:
            result = LinuxIXTalosHost(hostname, self, verbose=verbose)

        elif 'moz2-linux' in hostname or 'linux-ix' in hostname or \
             'try-linux' in hostname or 'linux64-ix-' in hostname or \
             'bld-centos' in hostname:
            result = LinuxBuildHost(hostname, self, verbose=verbose)

        elif 'try-mac' in hostname or 'xserve' in hostname or \
             'moz2-darwin' in hostname:
            result = OSXBuildHost(hostname, self, verbose=verbose)

        elif  '-r5-' in hostname or \
              '-r4-' in hostname:
            result = OSXPDUHost(hostname, self, verbose=verbose)

        elif 'tegra' in hostname:
            result = TegraHost(hostname, self, verbose=verbose)

        elif 'ec2-' in hostname:
            result = AWSHost(hostname, self, verbose=verbose)

        else:
            log.error("Unknown host type for %s", hostname)
            result = None

        if result is not None:
            result.wait()

        return result

    def loadTegras(self, toolspath):
        result = False
        tFile  = os.path.join(toolspath, 'devices.json')

        if os.path.isfile(tFile):
            try:
                self.tegras = json.load(open(tFile, 'r'))
                result = True
            except:
                log.error('error loading devices.json from %s' % tFile, exc_info=True)

        return result

    def rebootIfNeeded(self, host, lastSeen=None, indent='', dryrun=True, verbose=False):
        """ Reboot a host if needed. if lastSeen is None we will
            not attempt to reboot the host. """

        def graceful_shutdown_buildbot(host, indent, dryrun):
            failed = False
            if host.graceful_shutdown(indent=indent, dryrun=dryrun):
                if not dryrun:
                    log.info("%sWaiting for shutdown" % indent)
                    count = 0

                    while True:
                        count += 1
                        if count >= 30:
                            failed = True
                            log.info("%sTook too long to shut down; giving up" % indent)
                            break

                        data = host.tail_twistd_log(10)
                        if not data or "Main loop terminated" in data or "ProcessExitedAlready" in data:
                            break
            else:
                # failed graceful shutdown of buildbot client process
                failed = True
                log.info("%sgraceful_shutdown failed" % indent)
            return failed

        reboot      = False # was the host rebooted
        recovery    = False # did the host need recovery
        reachable   = False # is the host pingable
        ipmi        = False # does the host have an IPMI interface
        pdu         = False # does the host have a PDU interface
        failed      = False # set to True if a reboot succeeds
        should_reboot = False
        output      = []
        rebootHours = 6

        if host is None:
            self.debug('Host is None, returning')
            return { 'reboot': reboot, 'recovery': recovery, \
                'output': output, 'ipmi': ipmi, 'pdu': pdu, \
                'dryrun': dryrun }

        ipmi = host.hasIPMI
        pdu  = host.hasPDU
        reachable = host.reachable

        if not reachable:
            output.append(msg('adding to recovery list because host is not reachable', indent, verbose))
            recovery = True

        if lastSeen is None:
            output.append(msg('adding to recovery list because last activity is unknown', indent, verbose))
	    # don't set should_reboot because we want a manual recovery if no
	    # previous activity is known.
            recovery = True
        else:
            hours  = (lastSeen.days * 24) + (lastSeen.seconds / 3600)
            should_reboot = hours >= rebootHours
            recovery = should_reboot
            output.append(msg('last activity %0.2d hours' % hours, indent, verbose))

        # if we can ssh to host, then try and do normal shutdowns
        log.debug("recovery=%s, should_reboot=%s, reachable=%s" % (recovery, should_reboot, reachable))
        if recovery and should_reboot:
            if reachable:
                # attempt gracefull shutdown of buildbot client process
                graceful_shutdown_buildbot(host, indent, dryrun)
                if dryrun:
                    log.debug("would have soft-rebooted but dryrun is True")
                else:
                    failed = not host.reboot()
                    if failed:
                        log.info("soft reboot failed")
                    else:
                        log.info("soft reboot successful")
            if not reachable or failed:
                # not reachable; resort to stronger measures
                if dryrun:
                    log.debug("would have hard-rebooted but dryrun is True")
                else:
                    if not (host.hasPDU or host.hasIPMI):
                        log.info("unreachable host does not have PDU or IPMI support")
                    else:
                        if host.hasPDU:
                            pdu = host.rebootPDU()
                            if pdu == True:
                                log.info("PDU reboot successful")
                                failed = False
                                reboot = True
                            else:
                                log.info("PDU reboot not successful")
                                failed = True
                                output.append(msg('should be restarting but not reachable PDU reboot failed', indent, True))
                        if host.hasIPMI and not reboot:
                            ipmi = host.rebootIPMI()
                            if ipmi == True:
                                log.info("IPMI reboot successful")
                                failed = False
                                reboot = True
                            else:
                                log.info("IPMI reboot not successful")
                                failed = True
                                output.append(msg('should be restarting but not reachable and IPMI reboot failed', indent, True))

        return { 'reboot': reboot, 'recovery': recovery, 'output': output, 'ipmi': ipmi, 'pdu': pdu, 'dryrun': dryrun }

    def check(self, host, indent='', dryrun=True, verbose=False, reboot=False):
        status = { 'buildbot':  '',
                   'tacfile':   '',
                   'master':    '',
                   'fqdn':      '',
                   'reachable': False,
                   'lastseen':  None,
                   'output':    [],
                 }

        try:
            # default lastseen to buildapi's latest completed build time
            # it may be overridden by the date/time retrieved from twistd.log 
            status['lastseen'] = last_build_endtime(host.hostname)
            if status['lastseen'] != None:
                status['lastseen'] = datetime.now() - \
                    datetime.fromtimestamp(status['lastseen']).replace(
                    tzinfo=timezone('UTC')).astimezone( \
                    timezone('US/Pacific')).replace(tzinfo=None)
                log.debug('defaulting lastseen to %s' % status['lastseen'])
        except requests.exceptions.HTTPError:
            pass

        if host and host.fqdn:
            status['fqdn'] = host.fqdn

        if host is not None and host.reachable:
            status['reachable'] = host.reachable

            host.wait()

            tacfiles = host.find_buildbot_tacfiles()
            if "buildbot.tac" in tacfiles:
                status['tacfile'] = 'found'
                status['master']  = host.get_tacinfo()
            else:
                status['tacfile'] = 'NOT FOUND'
                if verbose:
                    log.info("%sbuildbot.tac NOT FOUND" % indent)

            if verbose:
                log.info("%sFound these tacfiles: %s" % (indent, tacfiles))
            for tac in tacfiles:
                m = re.match("^buildbot.tac.bug(\d+)$", tac)
                if m:
                    if verbose:
                        log.info("%sDisabled by bug %s" % (indent, m.group(1)))
                    status['tacfile'] = 'bug %s' % m.group(1)
                    break

            if host.buildbot_active():
                status['buildbot'] += '; running'
            else:
                status['buildbot'] += '; NOT running'

            data = host.tail_twistd_log(200)
            if len(data) > 0:
                lines = data.split('\n')
                logTD = None
                jobFound = None
                idleNote = None
                for line in reversed(lines):
                    if '[Broker,client]' in line:
                        if logTD is None:
                            logTD = getLogTimeDelta(line)
                        if not idleNote and (('commandComplete' in line) or ('startCommand' in line)):
                            jobFound = getLogTimeDelta(line)
                            break
                        if "rebooting NOW, since the master won't talk to us" in line:
                            idleNote = getLogTimeDelta(line)
                            break

                if logTD is None:
                    logTD = jobFound
                if logTD is not None:
                    status['lastseen'] = logTD
                    if (logTD.days == 0) and (logTD.seconds <= 3600):
                        status['buildbot'] += '; active'
                    if idleNote is not None:
                        status['buildbot'] += '; idle rebooted %s' % relative(idleNote)
                    if jobFound is not None:
                        status['buildbot'] += '; job %s' % relative(jobFound)

            data = host.tail_twistd_log(10)
            if "Stopping factory" in data:
                status['buildbot'] += '; factory stopped'
                if verbose:
                    log.info("%sLooks like the host isn't connected" % indent)
        else:
            log.error('%sUnable to control host remotely' % indent)

        if len(status['buildbot']) > 0:
            status['buildbot'] = status['buildbot'][2:]

        if status['reachable']:
            s = ''
            if status['tacfile'] != 'found':
                s += '; tacfile: %s' % status['tacfile']
            if len(status['buildbot']) > 0:
                s += '; buildbot: %s' % status['buildbot']
            if s.startswith('; '):
                s = s[2:]
        else:
            s = 'OFFLINE'

        if len(s) > 0:
            log.info('%s%s' % (indent, s))

        if reboot:
            d = self.rebootIfNeeded(host, lastSeen=status['lastseen'], indent=indent, dryrun=dryrun, verbose=verbose)
            for s in ['reboot', 'recovery', 'ipmi', 'pdu']:
                status[s] = d[s]
            status['output'] += d['output']

        return status

