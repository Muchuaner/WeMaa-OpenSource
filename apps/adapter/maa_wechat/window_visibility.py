"""Restore the calibrated official WeChat window inside the sole GUI worker.

EnumWindows also sees windows hidden to the tray, unlike Maa's visible-window
inventory. Continuous workers can also maximize the verified main window on
the calibrated desktop. Fresh geometry, layout, identity and send checks still
have to pass. This module never enters text or clicks a message.
"""
import ntpath
import time

WECHAT_EXE = r'D:\Weixin\Weixin.exe'
WECHAT_CLASS = 'Qt51514QWindowIcon'
CLIENT_SIZE = [1920, 1040]
DISPLAY_PROFILE = {'screen': [1920, 1080], 'work_area': [0, 0, 1920, 1040], 'dpi': 96}


def official(window):
    return (window.get('class') == WECHAT_CLASS
            and ntpath.normcase(ntpath.normpath(window.get('exe', '')))
            == ntpath.normcase(WECHAT_EXE))


def window_candidates(api):
    """Qt retains the dismissed storage dialog as a hidden, owned HWND.

    It cannot block an enabled main window. Keep every visible dialog, every
    disabled owner and every separate/ambiguous main process in the inventory.
    """
    windows = [window for window in api.windows() if official(window)]
    by_handle = {w['hwnd']: w for w in windows}
    def dormant_storage_prompt(window):
        owner = by_handle.get(window.get('owner'))
        size = (owner or {}).get('client')
        return (window.get('visible') is False and window.get('client') == [330, 348]
                and owner is not None and owner['pid'] == window['pid']
                and not owner.get('owner') and owner.get('enabled') is True
                and size is not None and size[0] >= 800 and size[1] >= 600)
    return [w for w in windows if not dormant_storage_prompt(w)]


def ensure_visible(*, restore_hidden=False, restore_geometry=False, expected_window=None,
                   api=None, probe=None, sleep=time.sleep):
    api = api or Win32Windows()
    windows = window_candidates(api)
    if len(windows) != 1:
        raise RuntimeError(f'WECHAT_WINDOW_NOT_UNIQUE:{len(windows)}')
    before = windows[0]
    if expected_window and (before['hwnd'] != expected_window['hwnd']
                            or before['pid'] != expected_window['pid']):
        raise RuntimeError('WECHAT_WINDOW_IDENTITY_CHANGED')
    if not before.get('enabled', True):
        raise RuntimeError('WECHAT_MAIN_WINDOW_DISABLED')
    changed = before['client'] != CLIENT_SIZE
    if changed and (not restore_geometry or not before['visible'] or before['minimized']):
        raise RuntimeError('LAYOUT_CHANGED_STOP')
    # An unbound, small login/dialog window is never promoted to the main chat.
    if changed and not expected_window and (not before['client']
            or before['client'][0] < 800 or before['client'][1] < 600):
        raise RuntimeError('WECHAT_MAIN_WINDOW_NOT_VERIFIED')
    if before['visible'] and not before['minimized'] and not changed:
        return {'restored': False, 'window': before}
    if not changed and not restore_hidden:
        raise RuntimeError('WECHAT_WINDOW_NOT_UNIQUE:0')
    if probe is None:
        from desktop_input import desktop_probe
        probe = desktop_probe
    if probe().get('available') is not True:
        raise RuntimeError('INPUT_DESKTOP_UNAVAILABLE')
    # Recheck ownership immediately before acting on a possibly reused HWND.
    current = api.inspect(before['hwnd'])
    if (not official(current) or current.get('pid') != before['pid']
            or current.get('client') != before['client']):
        raise RuntimeError('WECHAT_WINDOW_IDENTITY_CHANGED')
    if changed:
        if api.display_profile(before['hwnd']) != DISPLAY_PROFILE:
            raise RuntimeError('WECHAT_DISPLAY_NOT_CALIBRATED')
        api.maximize(before['hwnd'], expected_window=current)
    else:
        api.restore(before['hwnd'])
    sleep(1)
    after = api.inspect(before['hwnd'])
    if not official(after) or after.get('pid') != before['pid']:
        raise RuntimeError('WECHAT_WINDOW_IDENTITY_CHANGED')
    if (after.get('client') != CLIENT_SIZE or not after.get('visible')
            or after.get('minimized')):
        raise RuntimeError('LAYOUT_CHANGED_STOP')
    return {'restored': True, 'geometry_restored': changed, 'before': before, 'window': after}


class Win32Windows:
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes, self.types = ctypes, wintypes
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.user.EnumWindows.argtypes = [self.callback, wintypes.LPARAM]
        self.user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user.IsWindowEnabled.argtypes = [wintypes.HWND]
        self.user.IsIconic.argtypes = [wintypes.HWND]
        self.user.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user.GetWindow.restype = wintypes.HWND
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
            wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]

    def inspect(self, hwnd):
        ctypes, types = self.ctypes, self.types
        name = ctypes.create_unicode_buffer(256)
        self.user.GetClassNameW(hwnd, name, len(name))
        pid, rect = types.DWORD(), types.RECT()
        self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        valid = self.user.GetClientRect(hwnd, ctypes.byref(rect))
        exe = ''
        if name.value == WECHAT_CLASS:
            handle = self.kernel.OpenProcess(0x1000, False, pid.value)
            if handle:
                try:
                    buffer = ctypes.create_unicode_buffer(32768)
                    size = types.DWORD(len(buffer))
                    if self.kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                        exe = buffer.value
                finally:
                    self.kernel.CloseHandle(handle)
        return {'hwnd': int(hwnd), 'pid': pid.value, 'class': name.value, 'exe': exe,
                'client': [rect.right, rect.bottom] if valid else None,
                'visible': bool(self.user.IsWindowVisible(hwnd)),
                'enabled': bool(self.user.IsWindowEnabled(hwnd)),
                'owner': int(self.user.GetWindow(hwnd, 4) or 0),  # GW_OWNER
                'minimized': bool(self.user.IsIconic(hwnd))}

    def windows(self, *, include_tray=False):
        result = []
        @self.callback
        def collect(hwnd, _):
            window = self.inspect(hwnd)
            classes = (WECHAT_CLASS, 'Qt51514WxTrayIconMessageWindowClass', 'Shell_TrayWnd') if include_tray else (WECHAT_CLASS,)
            if window['class'] in classes:
                result.append(window)
            return True
        if not self.user.EnumWindows(collect, 0):
            raise RuntimeError('WECHAT_WINDOW_INVENTORY_FAILED')
        return result

    def restore(self, hwnd):
        window = self.inspect(hwnd)
        if not official(window) or window.get('client') != CLIENT_SIZE:
            raise RuntimeError('WECHAT_WINDOW_IDENTITY_CHANGED')
        # ShowWindow exposes a Qt-hidden HWND without restoring its view. Use
        # the actual process-owned tray icon, which restores the existing app.
        from window_tray import restore_from_tray
        restore_from_tray(window)

    def display_profile(self, hwnd):
        rect = self.types.RECT()
        self.user.SystemParametersInfoW.argtypes = [self.types.UINT, self.types.UINT,
            self.types.LPVOID, self.types.UINT]
        self.user.GetDpiForWindow.argtypes = [self.types.HWND]
        self.user.GetDpiForWindow.restype = self.types.UINT
        if not self.user.SystemParametersInfoW(0x0030, 0, self.ctypes.byref(rect), 0):
            raise RuntimeError('WECHAT_DISPLAY_PROFILE_UNAVAILABLE')
        return {'screen': [self.user.GetSystemMetrics(0), self.user.GetSystemMetrics(1)],
                'work_area': [rect.left, rect.top, rect.right, rect.bottom],
                'dpi': self.user.GetDpiForWindow(hwnd)}

    def maximize(self, hwnd, *, expected_window):
        window = self.inspect(hwnd)
        if (not official(window) or not window['visible'] or window['minimized']
                or window['pid'] != expected_window['pid']
                or window['client'] != expected_window['client']):
            raise RuntimeError('WECHAT_WINDOW_IDENTITY_CHANGED')
        self.user.ShowWindowAsync.argtypes = [self.types.HWND, self.ctypes.c_int]
        if not self.user.ShowWindowAsync(hwnd, 3):  # SW_MAXIMIZE, existing process only.
            raise RuntimeError('WECHAT_MAXIMIZE_REQUEST_FAILED')
