"""Keep a continuous worker alive while its desktop is temporarily unavailable.

The runtime factory can restore the verified official window's visibility.
Recovery then requires two valid fresh frames. It never clicks messages, inputs
text, or retries a send. Re-entry retains the same process's activation and
verified baselines; an interrupted send remains uncertain in durable queues.
"""
import time

import liveness
from layout import check_frame

RECOVERY_INTERVAL_SECONDS = 5


class ContinuousActivation:
    """In-memory continuity for window recovery, never for a process restart."""
    def __init__(self):
        self.state = None

    def restore(self, run_id, now):
        if self.state is None or self.state[0] != run_id or not 0 < self.state[1] <= now:
            return None
        return self.state[1:]

    def remember(self, run_id, enabled_at, baselines, attempted):
        # The sets belong to this continuous watch; later verified additions
        # remain available when only the controller has to be reconstructed.
        self.state = (run_id, enabled_at, baselines, attempted)


def recoverable(exc):
    reason = str(exc)
    return isinstance(exc, RuntimeError) and (
        reason in ('INPUT_DESKTOP_UNAVAILABLE','LAYOUT_CHANGED_STOP', 'MAA_BLACK_FRAME',
                   'MAA_CAPTURE_UNAVAILABLE', 'WECHAT_WINDOW_NOT_UNIQUE:0', 'WECHAT_TRAY_NOT_VERIFIED',
                   'WECHAT_MAIN_WINDOW_DISABLED', 'WECHAT_STORAGE_DIALOG_NOT_VERIFIED',
                   'WECHAT_STORAGE_DIALOG_DISMISS_NOT_VERIFIED')
        or reason.startswith('MAA_CAPTURE_DIMENSIONS:'))


def run_watch_with_recovery(factory, execute, notify, *, sleep=time.sleep):
    runtime = None
    count = 0
    while True:
        try:
            runtime = runtime or factory()
            return execute(runtime)
        except RuntimeError as exc:
            if not recoverable(exc):
                raise
            reason = str(exc)
        count += 1
        stable = 0
        runtime = None
        while stable < 2:
            liveness.touch()
            notify({'status': 'waiting_for_input' if reason=='INPUT_DESKTOP_UNAVAILABLE' else 'waiting_for_window', 'error': reason,
                    'recovery_count': count, 'stable_checks': stable,
                    'sending_enabled': False, 'auto_reply_enabled': False})
            sleep(RECOVERY_INTERVAL_SECONDS)
            try:
                # Keep only the latest verified controller. No stale HWND/frame.
                candidate = factory()
                check_frame(candidate.capture_image())
                if reason=='INPUT_DESKTOP_UNAVAILABLE':
                    from desktop_input import desktop_probe
                    if desktop_probe().get('available') is not True:
                        stable=0;continue
            except (RuntimeError, ValueError) as exc:
                if not recoverable(exc) and str(exc) not in (
                        'DIVIDER_NOT_VERIFIABLE', 'FRAME_GEOMETRY_CHANGED') and not str(exc).startswith('DIVIDER_CHANGED:'):
                    raise
                reason = str(exc)
                stable = 0
                continue
            stable += 1
            runtime = candidate
        notify({'status': 'window_recovered', 'recovery_count': count,
                'stable_checks': stable, 'sending_enabled': False,
                'auto_reply_enabled': False})
