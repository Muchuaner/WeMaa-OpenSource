"""Maa activation of the native tray icon belonging to the existing WeChat PID."""
import time


def click_position(target, main_pid):
    icon, bar = target.get('icon'), target.get('bar')
    if (target.get('pid') != main_pid or bar != [0, 1040, 1920, 1080]
            or not isinstance(icon, list) or len(icon) != 4
            or any(type(v) is not int for v in icon)):
        raise RuntimeError('WECHAT_TRAY_NOT_VERIFIED')
    left, top, right, bottom = icon
    if not (0 <= left < right <= 1920 and 1040 <= top < bottom <= 1080
            and 16 <= right-left <= 40 and 16 <= bottom-top <= 40):
        raise RuntimeError('WECHAT_TRAY_NOT_VERIFIED')
    return (left+right)//2, (top+bottom)//2-1040


def restore_from_tray(window, *, api=None, probe=None, sleep=time.sleep):
    import numpy as np
    if probe is None:
        from desktop_input import desktop_probe
        probe=desktop_probe
    api=api or Win32Tray(window)
    target=api.locate()
    point=click_position(target,window['pid'])
    first, second=api.capture(target),api.capture(target)
    left,top,right,bottom=target['icon'];top-=1040;bottom-=1040
    if (first is None or second is None or first.shape[:2]!=(40,1920)
            or second.shape!=first.shape or len(first.shape)!=3
            or first.shape[2] not in (3,4)
            or not np.array_equal(first[top:bottom,left:right,:3],second[top:bottom,left:right,:3])):
        raise RuntimeError('WECHAT_TRAY_NOT_VERIFIED')
    # Recheck the native HWND/PID/icon rectangle just before actual input.
    if api.locate()!=target:raise RuntimeError('WECHAT_TRAY_NOT_VERIFIED')
    if probe().get('available') is not True:raise RuntimeError('INPUT_DESKTOP_UNAVAILABLE')
    for _ in range(2):
        api.click(target,*point)
        sleep(.08)
    # Leaving the cursor over WeChat's tray icon opens an unread-preview popup
    # that correctly blocks message copying. Move away without another click.
    api.leave_icon(target)


class Win32Tray:
    def __init__(self, window):
        self.window=window
        self.controller=None

    def locate(self):
        import ctypes
        from ctypes import wintypes
        from window_visibility import Win32Windows,official,CLIENT_SIZE
        api=Win32Windows();current=api.inspect(self.window['hwnd'])
        if (not official(current) or current['pid']!=self.window['pid']
                or current['client']!=CLIENT_SIZE):
            raise RuntimeError('WECHAT_WINDOW_IDENTITY_CHANGED')
        windows=api.windows(include_tray=True)
        trays=[w for w in windows if w['class']=='Qt51514WxTrayIconMessageWindowClass'
               and w['pid']==current['pid']]
        bars=[w for w in windows if w['class']=='Shell_TrayWnd']
        if len(trays)!=1 or len(bars)!=1:raise RuntimeError('WECHAT_TRAY_NOT_VERIFIED')
        class GUID(ctypes.Structure):
            _fields_=[('a',wintypes.DWORD),('b',wintypes.WORD),('c',wintypes.WORD),('d',ctypes.c_byte*8)]
        class Identifier(ctypes.Structure):
            _fields_=[('size',wintypes.DWORD),('hwnd',wintypes.HWND),('uid',wintypes.UINT),('guid',GUID)]
        shell=ctypes.WinDLL('shell32',use_last_error=True)
        shell.Shell_NotifyIconGetRect.argtypes=[ctypes.POINTER(Identifier),ctypes.POINTER(wintypes.RECT)]
        ident=Identifier();ident.size=ctypes.sizeof(ident);ident.hwnd=trays[0]['hwnd'];ident.uid=0
        icon,bar=wintypes.RECT(),wintypes.RECT()
        if shell.Shell_NotifyIconGetRect(ctypes.byref(ident),ctypes.byref(icon))!=0:
            raise RuntimeError('WECHAT_TRAY_NOT_VERIFIED')
        api.user.GetWindowRect.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.RECT)]
        if not api.user.GetWindowRect(bars[0]['hwnd'],ctypes.byref(bar)):
            raise RuntimeError('WECHAT_TRAY_NOT_VERIFIED')
        return {'pid':current['pid'],'tray_hwnd':trays[0]['hwnd'],'bar_hwnd':bars[0]['hwnd'],
                'icon':[icon.left,icon.top,icon.right,icon.bottom],
                'bar':[bar.left,bar.top,bar.right,bar.bottom]}

    def capture(self,target):
        if self.controller is None:
            from maa.controller import Win32Controller,MaaWin32ScreencapMethodEnum as C,MaaWin32InputMethodEnum as I
            self.controller=Win32Controller(target['bar_hwnd'],C.PrintWindow,I.Seize,I.Seize)
            self.controller.set_screenshot_use_raw_size(True)
            if not self.controller.post_connection().wait().succeeded:
                raise RuntimeError('WECHAT_TRAY_NOT_VERIFIED')
        return self.controller.post_screencap().wait().get()

    def click(self,target,x,y):
        if not self.controller.post_click(x,y).wait().succeeded:
            raise RuntimeError('WECHAT_TRAY_NOT_VERIFIED')

    def leave_icon(self,target):
        if not self.controller.post_touch_move(800,20).wait().succeeded:
            raise RuntimeError('WECHAT_TRAY_NOT_VERIFIED')
