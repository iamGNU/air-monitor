"""How hackrf_transfer is started and stopped: on Linux/macOS through its stdout, on Windows through a named pipe.
The Windows API can't run here, so it is simulated: these tests check the logic, not Windows itself."""
import io
import os
import signal
import subprocess
import sys
import threading
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import air_monitor as am  # noqa: E402

PIPE_HANDLE = 1234


class FakeProc:
    def __init__(self, running=True, hangs=False):
        self.running, self.hangs = running, hangs
        self.signals, self.waits, self.killed = [], 0, False
        self.stderr = io.BytesIO(b"")

    def poll(self):
        return None if self.running else 1

    def send_signal(self, sig):
        self.signals.append(sig)

    def wait(self, timeout=None):
        self.waits += 1
        if self.hangs and not self.killed:
            raise subprocess.TimeoutExpired("hackrf_transfer", timeout)
        return 0

    def kill(self):
        self.killed = True


class Command(unittest.TestCase):
    def test_command(self):
        self.assertEqual(am.hackrf_command(16, 20), ["hackrf_transfer", "-r", "-", "-f", "2438500000", "-s",
                                                     "6500000", "-a", "0", "-l", "16", "-g", "20"])
        cmd = am.hackrf_command(8, 10, r"\\.\pipe\x", ["-n", "325000"])
        self.assertEqual(cmd[1:3], ["-r", r"\\.\pipe\x"])
        self.assertEqual(cmd[-2:], ["-n", "325000"])


class Stop(unittest.TestCase):
    def stop(self, os_name, proc):
        src = io.BytesIO()
        with mock.patch.object(am.os, "name", os_name):
            am.stop_hackrf(proc, src)
        self.assertTrue(src.closed)
        return proc

    def test_posix_sigint(self):
        p = self.stop("posix", FakeProc())
        self.assertEqual(p.signals, [signal.SIGINT])
        self.assertFalse(p.killed)

    def test_windows_closes_the_pipe_only(self):
        p = self.stop("nt", FakeProc())                  # SIGINT would raise ValueError on Windows
        self.assertEqual(p.signals, [])
        self.assertFalse(p.killed)

    def test_already_exited(self):
        self.assertEqual(self.stop("posix", FakeProc(running=False)).signals, [])

    def test_killed_if_it_hangs(self):
        p = self.stop("posix", FakeProc(hangs=True))
        self.assertTrue(p.killed)


class LinuxStart(unittest.TestCase):
    def test_reads_stdout(self):
        made = {}

        def popen(cmd, **kw):
            made.update(cmd=cmd, kw=kw)
            return types.SimpleNamespace(stdout=io.BytesIO(b""), stderr=io.BytesIO(b""))
        with mock.patch.object(am.os, "name", "posix"), mock.patch.object(am.subprocess, "Popen", popen):
            proc, src = am.open_hackrf(16, 20)
        self.assertEqual(made["cmd"][1:3], ["-r", "-"])
        self.assertIs(src, proc.stdout)                  # (no fileno on BytesIO: the pipe-size tweak is skipped)


class WindowsStart(unittest.TestCase):
    """_open_hackrf_windows with kernel32, msvcrt and Popen replaced by fakes."""

    @staticmethod
    def close_quietly(fd):
        try:
            os.close(fd)
        except OSError:                                  # already closed with the file object
            pass

    def open(self, exits_early):
        r, w = os.pipe()
        for fd in (r, w):
            self.addCleanup(self.close_quietly, fd)
        own_connect = threading.Event()
        k32 = mock.MagicMock()
        k32.CreateNamedPipeW.return_value = PIPE_HANDLE

        def connect(handle, overlapped):                 # blocks until a client opens the pipe
            if exits_early:
                own_connect.wait(5)                      # ... which is us, as hackrf_transfer never does
            return True
        k32.ConnectNamedPipe.side_effect = connect
        k32.CreateFileW.side_effect = lambda *a: (own_connect.set(), 99)[1]
        made = {}

        def popen(cmd, **kw):
            made.update(cmd=cmd, kw=kw)
            return FakeProc(running=not exits_early)
        msvcrt = types.SimpleNamespace(open_osfhandle=lambda handle, flags: r)
        with mock.patch.object(am.os, "name", "nt"), \
                mock.patch("ctypes.WinDLL", create=True, return_value=k32), \
                mock.patch("ctypes.get_last_error", create=True, return_value=0), \
                mock.patch.dict(sys.modules, {"msvcrt": msvcrt}), \
                mock.patch.object(am.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), \
                mock.patch.object(am.subprocess, "Popen", popen):
            proc, src = am.open_hackrf(16, 20)
        return proc, src, made, k32, w

    def test_streams_through_a_named_pipe(self):
        proc, src, made, k32, w = self.open(exits_early=False)
        name = made["cmd"][2]
        self.assertTrue(name.startswith("\\\\.\\pipe\\air-monitor-"), name)
        self.assertEqual(k32.CreateNamedPipeW.call_args[0][:2], (name, 0x1))           # the same pipe, inbound
        self.assertEqual(made["kw"]["stdout"], subprocess.DEVNULL)
        self.assertEqual(made["kw"]["creationflags"], 0x08000000)                     # no console window
        os.write(w, b"\x0a\x0d\x00\xff")                                               # bytes arrive unchanged
        self.assertEqual(src.read(4), b"\x0a\x0d\x00\xff")
        src.close()

    def test_gives_up_cleanly_without_a_hackrf(self):
        proc, src, made, k32, w = self.open(exits_early=True)
        self.assertEqual(src.read(), b"")                # empty: the receiver then reports stderr's error
        k32.CreateFileW.assert_called_once()
        self.assertEqual(k32.CreateFileW.call_args[0][0], made["cmd"][2])
        k32.CloseHandle.assert_any_call(PIPE_HANDLE)


if __name__ == "__main__":
    unittest.main()
