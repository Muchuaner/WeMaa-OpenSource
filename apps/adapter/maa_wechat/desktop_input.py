"""Read-only input-desktop checks inside the existing interactive GUI worker.

No cursor movement, desktop switching, session reconnect, unlock or credentials.
The background admin agent must consume this worker's evidence, not probe its
own Session 0 cursor. Readable input is a necessary gate, not a send receipt.
"""
import os
import sys
import time


def desktop_probe():
    result = {'checked_at': time.time(), 'process_id': os.getpid(), 'session_id': None,
              'cursor_readable': False, 'input_desktop_accessible': False,
              'input_desktop_name': None, 'thread_desktop_name': None,
              'desktop_matches': False, 'available': False, 'last_error': None,
              'reason': 'INPUT_DESKTOP_UNVERIFIED'}
    if sys.platform != 'win32':
        return dict(result, reason='INPUT_DESKTOP_UNSUPPORTED_PLATFORM')
    import ctypes
    from ctypes import wintypes
    user = ctypes.WinDLL('user32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    user.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user.GetCursorPos.restype = wintypes.BOOL
    user.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user.OpenInputDesktop.restype = wintypes.HANDLE
    user.GetThreadDesktop.argtypes = [wintypes.DWORD]
    user.GetThreadDesktop.restype = wintypes.HANDLE
    user.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int,
        wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    user.GetUserObjectInformationW.restype = wintypes.BOOL
    user.CloseDesktop.argtypes = [wintypes.HANDLE]
    user.CloseDesktop.restype = wintypes.BOOL
    kernel.GetCurrentThreadId.restype = wintypes.DWORD
    kernel.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    kernel.ProcessIdToSessionId.restype = wintypes.BOOL
    session = wintypes.DWORD()
    ctypes.set_last_error(0)
    if not kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
        return dict(result, reason='INPUT_SESSION_UNVERIFIED', last_error=ctypes.get_last_error())
    result['session_id'] = int(session.value)
    if session.value == 0:
        return dict(result, reason='INPUT_WORKER_IN_SESSION_ZERO')
    point = wintypes.POINT()
    ctypes.set_last_error(0)
    result['cursor_readable'] = bool(user.GetCursorPos(ctypes.byref(point)))
    cursor_error = ctypes.get_last_error() if not result['cursor_readable'] else None
    ctypes.set_last_error(0)
    handle = user.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS only
    if not handle:
        error = ctypes.get_last_error()
        return dict(result, reason='INPUT_DESKTOP_ACCESS_DENIED' if error == 5 else 'INPUT_DESKTOP_OPEN_FAILED',
                    last_error=error or cursor_error)
    try:
        def name(desktop):
            buffer = ctypes.create_unicode_buffer(256)
            needed = wintypes.DWORD()
            ctypes.set_last_error(0)
            if not desktop or not user.GetUserObjectInformationW(desktop, 2, buffer,
                    ctypes.sizeof(buffer), ctypes.byref(needed)):  # UOI_NAME
                raise OSError(ctypes.get_last_error(), 'INPUT_DESKTOP_NAME_UNAVAILABLE')
            return buffer.value
        result['input_desktop_name'] = name(handle)
        result['input_desktop_accessible'] = True
        result['thread_desktop_name'] = name(user.GetThreadDesktop(kernel.GetCurrentThreadId()))
    except OSError as exc:
        return dict(result, reason='INPUT_DESKTOP_NAME_UNAVAILABLE', last_error=exc.errno)
    finally:
        user.CloseDesktop(handle)
    result['desktop_matches'] = result['input_desktop_name'] == result['thread_desktop_name']
    if not result['cursor_readable']:
        return dict(result, reason='CURSOR_ACCESS_DENIED' if cursor_error == 5 else 'CURSOR_READ_FAILED',
                    last_error=cursor_error)
    if not result['desktop_matches']:
        return dict(result, reason='INPUT_DESKTOP_MISMATCH')
    if result['input_desktop_name'] != 'Default':
        return dict(result, reason='INPUT_DESKTOP_NOT_DEFAULT')
    return dict(result, available=True, reason='INPUT_DESKTOP_READABLE')


class InputDesktopGate:
    def __init__(self, probe=None, *, clock=None):
        self.probe = probe or desktop_probe
        self.clock = clock or time.time
        self.stable_checks = 0
        self.identity = None
        self.ready = False

    def check(self, runtime, run_id):
        try:
            evidence = dict(self.probe())
        except Exception as exc:
            evidence = {'available': False, 'reason': 'INPUT_DESKTOP_PROBE_FAILED',
                        'probe_error_type': type(exc).__name__}
        evidence.update(run_id=run_id, checked_at=self.clock(), ready=False,
                        stable_checks=self.stable_checks, layout_verified=False)
        identity = (evidence.get('session_id'), evidence.get('input_desktop_name'),
                    evidence.get('thread_desktop_name'))
        if (evidence.get('available') is not True or type(identity[0]) is not int
                or identity[0] <= 0 or identity[1:] != ('Default', 'Default')
                or evidence.get('cursor_readable') is not True
                or evidence.get('input_desktop_accessible') is not True
                or evidence.get('desktop_matches') is not True):
            self.stable_checks = 0; self.ready = False; self.identity = None
            return dict(evidence, stable_checks=0)
        if identity != self.identity:
            self.stable_checks = 0; self.ready = False; self.identity = identity
        try:
            prepare = getattr(runtime, 'prepare_input', None)
            repaired = prepare() if callable(prepare) else None
            if repaired and repaired.get('restored'):
                self.stable_checks = 0; self.ready = False
            if not self.ready:
                from layout import check_frame
                check_frame(runtime.capture_image())
                self.stable_checks += 1
                self.ready = self.stable_checks >= 2
        except Exception as exc:
            self.stable_checks = 0; self.ready = False
            return dict(evidence, reason='INPUT_LAYOUT_NOT_VERIFIED',
                        layout_error_type=type(exc).__name__, layout_error=str(exc)[:160], stable_checks=0)
        return dict(evidence, ready=self.ready, stable_checks=self.stable_checks,
                    layout_verified=True,
                    reason='INPUT_DESKTOP_READY' if self.ready else 'INPUT_DESKTOP_CONFIRMING')
