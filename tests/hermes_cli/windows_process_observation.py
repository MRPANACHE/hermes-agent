"""Native Windows observations for disposable process-containment tests."""
import os

import psutil


def assert_initial_threads_suspended(pid, *, expected_parent=None):
    """Read the prior kernel suspend count and restore it without running a thread."""
    import ctypes
    from ctypes import wintypes

    process = psutil.Process(pid)
    parent = os.getpid() if expected_parent is None else expected_parent
    assert process.ppid() == parent, f'expected parent {parent}, got {process.ppid()}'
    threads = process.threads()
    assert threads, 'the newly created child has no initial thread'
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenThread.restype = wintypes.HANDLE
    api.SuspendThread.argtypes = [wintypes.HANDLE]
    api.SuspendThread.restype = wintypes.DWORD
    api.ResumeThread.argtypes = [wintypes.HANDLE]
    api.ResumeThread.restype = wintypes.DWORD
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    failed = 0xFFFFFFFF
    for thread in threads:
        handle = api.OpenThread(0x0002, False, thread.id)  # THREAD_SUSPEND_RESUME
        assert handle, ctypes.WinError(ctypes.get_last_error())
        previous = failed
        try:
            previous = api.SuspendThread(handle)
            assert previous != failed, ctypes.WinError(ctypes.get_last_error())
            assert previous > 0, 'child thread was running before containment'
        finally:
            try:
                if previous != failed:
                    assert api.ResumeThread(handle) == previous + 1
            finally:
                assert api.CloseHandle(handle)
