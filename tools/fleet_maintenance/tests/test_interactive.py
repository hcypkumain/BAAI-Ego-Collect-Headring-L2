import contextlib
import io
from pathlib import Path
import sys
import unittest
from unittest import mock

import pexpect
BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))
import online

class Child:
    def __init__(self,events):
        self.events=list(events); self.before=''; self.sent=[]; self.logfile='unset'; self.exitstatus=0; self.signalstatus=None; self.alive=True
    def expect(self,patterns):
        index,text=self.events.pop(0); self.before=text; return index
    def sendline(self,value): self.sent.append(value)
    def close(self,force=False): self.alive=False
    def isalive(self): return self.alive

def ssh_fixture():
    with mock.patch.object(online,'config_for',return_value={'user':'orangepi','hostname':'192.168.1.103'}):
        return online.SSH(Path('/fixture/config'),'cp_0014','192.168.20.111')

class InteractiveTests(unittest.TestCase):
    def test_real_local_pty_prompt_exchange(self):
        script=("import sys; print(\"fixture@host's password:\",end='',flush=True); sys.stdin.readline(); "
                "print('"+online.SUDO_PROMPT+"',end='',flush=True); sys.stdin.readline(); print('transaction-complete',flush=True)")
        output=io.StringIO()
        with mock.patch.object(online.getpass,'getpass',side_effect=['fixture-ssh','fixture-sudo']), contextlib.redirect_stdout(output):
            online.interactive_ssh([sys.executable,'-c',script])
        self.assertIn('transaction-complete',output.getvalue())
        self.assertNotIn('fixture-ssh',output.getvalue()); self.assertNotIn('fixture-sudo',output.getvalue())

    def test_local_pty_ssh_no_remote_pty_and_credentials_not_logged(self):
        child=Child([(1,'connecting\n'),(0,'ready\n'),(4,'done SSH_TEST_SECRET SUDO_TEST_SECRET\n')])
        ssh=ssh_fixture(); out=io.StringIO()
        with mock.patch.object(ssh,'require_tty'), mock.patch.object(pexpect,'spawn',return_value=child) as spawn, \
             mock.patch.object(online.getpass,'getpass',side_effect=['SSH_TEST_SECRET','SUDO_TEST_SECRET']) as prompt, \
             mock.patch.object(online.subprocess,'run',side_effect=AssertionError('interactive subprocess used')), \
             contextlib.redirect_stdout(out):
            ssh.run("sudo sh -c 'true'",interactive=True)
        executable,args=spawn.call_args.args
        self.assertEqual(executable,'ssh'); self.assertIn('-T',args); self.assertNotIn('-tt',args)
        self.assertEqual(spawn.call_args.kwargs['echo'],False); self.assertIsNone(child.logfile)
        self.assertNotIn('env',spawn.call_args.kwargs)
        self.assertEqual(child.sent,['SSH_TEST_SECRET','SUDO_TEST_SECRET']); self.assertEqual(prompt.call_count,2)
        self.assertIn('sudo -S -p '+online.SUDO_PROMPT,args[-1])
        self.assertNotIn('SSH_TEST_SECRET',repr(spawn.call_args)); self.assertNotIn('SUDO_TEST_SECRET',repr(spawn.call_args))
        self.assertNotIn('SSH_TEST_SECRET',out.getvalue()); self.assertNotIn('SUDO_TEST_SECRET',out.getvalue())

    def test_repeat_each_auth_prompt_stops_without_second_password(self):
        for first in (0,1,2):
            child=Child([(first,''),(first,'')])
            with self.subTest(first=first), mock.patch.object(pexpect,'spawn',return_value=child), \
                 mock.patch.object(online.getpass,'getpass',return_value='test-only') as prompt:
                with self.assertRaisesRegex(online.Failure,'prompted more than once'):
                    online.interactive_ssh(['ssh','-T','host','sudo -S true'])
            self.assertEqual(prompt.call_count,1); self.assertEqual(len(child.sent),1); self.assertFalse(child.alive)

    def test_permission_denied_stops_before_password(self):
        child=Child([(3,'')])
        with mock.patch.object(pexpect,'spawn',return_value=child), mock.patch.object(online.getpass,'getpass') as prompt:
            with self.assertRaisesRegex(online.Failure,'denied'): online.interactive_ssh(['ssh','-T','host','x'])
        prompt.assert_not_called()

    def test_failed_exit_and_invalid_root_command(self):
        child=Child([(4,'')]); child.exitstatus=1
        with mock.patch.object(pexpect,'spawn',return_value=child):
            with self.assertRaisesRegex(online.Failure,'failed'): online.interactive_ssh(['ssh','-T','host','x'])
        for cmd in ('sh -c true','sudo\nsh',' sudo sh','sudo sh\ntrue'):
            with self.assertRaises(online.Failure): online.sudo_stdin_command(cmd)

    def test_non_tty_fails_with_actionable_message(self):
        ssh=ssh_fixture()
        with mock.patch.object(online.sys.stdin,'isatty',return_value=False):
            with self.assertRaisesRegex(online.Failure,'--sudo-command-file'): ssh.run('sudo true',interactive=True)

if __name__=='__main__': unittest.main()
