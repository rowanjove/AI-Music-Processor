"""Optional process-session actions; power actions require batch-scoped consent.

No state is persisted, and importing this module performs no system action.
"""
import ctypes
import contextlib
import base64
import json
import os
import subprocess
import threading
import uuid
import xml.etree.ElementTree as ET

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _execution_state_api():
    if os.name != 'nt':
        return None
    api = ctypes.WinDLL('kernel32', use_last_error=True).SetThreadExecutionState
    api.argtypes, api.restype = [ctypes.c_uint32], ctypes.c_uint32
    return api


class SleepGuard:
    def __init__(self, enabled=False, api=None):
        self.enabled, self.api = bool(enabled), api
        self.active, self.reason = False, 'disabled'
        self._thread, self._previous = None, None

    def __enter__(self):
        if self.active:
            raise RuntimeError('Sleep guard is already active')
        if self.enabled:
            self.api = self.api or _execution_state_api()
            if self.api is None:
                self.reason = 'unsupported_platform'
                return self
            previous = self.api(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
            if not previous:
                raise OSError('SetThreadExecutionState failed')
            self._previous, self._thread = previous, threading.get_ident()
            self.active, self.reason = True, 'system_idle_sleep_prevented'
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.active:
            if self._thread != threading.get_ident():
                raise RuntimeError('Sleep guard must be restored on its entering thread')
            restored = self.api(self._previous | ES_CONTINUOUS)
            self.active = False
            if not restored:
                raise OSError('Could not restore thread execution state')
        return False


_TOAST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
Add-Type -AssemblyName System.Runtime.WindowsRuntime
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
[Windows.UI.Notifications.ToastNotifier, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
[Windows.UI.Notifications.NotificationSetting, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
[Windows.UI.Notifications.ToastNotificationHistory, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
[Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] | Out-Null
$appId = 'AI.Music.Processor'
$registration = 'HKCU:\Software\Classes\AppUserModelId\' + $appId
New-Item -Path $registration -Force | Out-Null
if (-not (Get-ItemProperty -Path $registration -Name DisplayName -ErrorAction SilentlyContinue)) {
  New-ItemProperty -Path $registration -Name DisplayName -Value 'AI Music Processor' -PropertyType String | Out-Null
}
$xml = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:AI_MUSIC_PROCESSOR_TOAST_XML))
$document = [Windows.Data.Xml.Dom.XmlDocument]::new()
$document.LoadXml($xml)
$toast = [Windows.UI.Notifications.ToastNotification]::new($document)
$toast.Tag = $env:AI_MUSIC_PROCESSOR_TOAST_TAG
$toast.Group = 'processor'
$toast.ExpirationTime = [DateTimeOffset]::Now.AddMinutes(5)
$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId)
$setting = 'UNKNOWN'
$nativeSetting = $notifier.Setting
if ($null -ne $nativeSetting) {
  $settings = @{0='Enabled';1='DisabledForApp';2='DisabledForUser';3='DisabledByGroupPolicy';4='DisabledByManifest'}
  $setting = $settings[[int]$nativeSetting]
  if ($null -eq $setting) { $setting = 'UNKNOWN' }
}
if ($setting -ne 'UNKNOWN' -and $setting -ne 'Enabled') {
  @{status='DISABLED_BY_SYSTEM_POLICY';history_confirmed=$false;setting=$setting;app_id=$appId} | ConvertTo-Json -Compress
  exit 0
}
$notifier.Show($toast)
$confirmed = $false
for ($attempt=0; $attempt -lt 10; $attempt++) {
  $history = [Windows.UI.Notifications.ToastNotificationManager]::History.GetHistory($appId)
  if (@($history | Where-Object { $_.Tag -eq $toast.Tag -and $_.Group -eq 'processor' }).Count -gt 0) {
    $confirmed = $true
    break
  }
  Start-Sleep -Milliseconds 100
}
@{status='SUBMITTED';history_confirmed=$confirmed;setting=$setting;app_id=$appId;tag=$toast.Tag} | ConvertTo-Json -Compress
"""


def send_windows_toast(title, message, runner=None):
    """Submit an own-AUMID native Toast and verify its own notification history."""
    if os.name != 'nt' and runner is None:
        return {'status': 'UNSUPPORTED_PLATFORM', 'history_confirmed': False}
    if type(title) is not str or type(message) is not str or len(title) > 256 or len(message) > 2048:
        raise ValueError('Toast title/message exceed bounded text policy')
    root = ET.Element('toast')
    visual = ET.SubElement(root, 'visual')
    binding = ET.SubElement(visual, 'binding', template='ToastGeneric')
    ET.SubElement(binding, 'text').text = title
    ET.SubElement(binding, 'text').text = message
    ET.SubElement(root, 'audio', silent='true')
    env = dict(os.environ)
    env['AI_MUSIC_PROCESSOR_TOAST_XML'] = base64.b64encode(ET.tostring(root, encoding='utf-8')).decode('ascii')
    env['AI_MUSIC_PROCESSOR_TOAST_TAG'] = uuid.uuid4().hex[:16]
    windows = os.environ.get('WINDIR', r'C:\Windows')
    command = [os.path.join(windows, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe'),
               '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', _TOAST_SCRIPT]
    try:
        result = (runner or subprocess.run)(command, env=env, stdin=subprocess.DEVNULL,
                                            capture_output=True, text=True, encoding='utf-8', errors='replace',
                                            timeout=15, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if result.returncode:
            return {'status': 'FAILED', 'history_confirmed': False, 'error': result.stderr[-1000:]}
        payload = json.loads(result.stdout.strip())
        if not isinstance(payload, dict) or payload.get('app_id') != 'AI.Music.Processor':
            raise ValueError('Unexpected native Toast receipt')
        return payload
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        return {'status': 'FAILED', 'history_confirmed': False, 'error': type(error).__name__}


def notify_success(callback=None, beep=True, toast=False, notifier=None):
    """A local UI callback and optional Windows notification sound; no messaging."""
    result = {'ui_notified': False, 'sound': False}
    if callback is not None:
        callback('处理完成', '本批次任务已全部完成，产物已保存。')
        result['ui_notified'] = True
    if toast:
        result['toast'] = (notifier or send_windows_toast)('处理完成', '本批次任务已全部完成，产物已保存。')
    if beep and os.name == 'nt':
        api = ctypes.WinDLL('user32', use_last_error=True).MessageBeep
        api.argtypes, api.restype = [ctypes.c_uint32], ctypes.c_int
        result['sound'] = bool(api(0x40))
    return result


def notify_failure(callback=None, toast=False, notifier=None):
    result = {'ui_notified': False}
    if callback is not None:
        callback('处理失败', '本批次包含失败或取消的任务；已完成产物保留，请查看日志。')
        result['ui_notified'] = True
    if toast:
        result['toast'] = (notifier or send_windows_toast)('处理失败', '本批次包含失败或取消的任务；已完成产物保留，请查看日志。')
    return result


def _run_power_action(action):
    if os.name != 'nt':
        raise RuntimeError('Power actions require Windows')
    if action in ('shutdown', 'abort_shutdown'):
        # shutdown.exe /t > 0 implicitly forces applications closed, even
        # without /f. The native API keeps the cancellation window while
        # explicitly preserving applications with unsaved work.
        library = ctypes.WinDLL('advapi32', use_last_error=True)
        if action == 'shutdown':
            api = library.InitiateSystemShutdownExW
            api.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32,
                            ctypes.c_int, ctypes.c_int, ctypes.c_uint32]
            api.restype = ctypes.c_int
            with _shutdown_privilege():
                if not api(None, 'AI Music Processor: confirmed batch completed', 60, False, False, 0x80040000):
                    raise OSError('Could not schedule a graceful local shutdown')
        else:
            api = library.AbortSystemShutdownW
            api.argtypes, api.restype = [ctypes.c_wchar_p], ctypes.c_int
            with _shutdown_privilege():
                if not api(None) and ctypes.get_last_error() != 1116:
                    raise OSError('Could not cancel this session scheduled shutdown')
    elif action == 'sleep':
        api = ctypes.WinDLL('powrprof', use_last_error=True).SetSuspendState
        api.argtypes, api.restype = [ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_ubyte], ctypes.c_ubyte
        with _shutdown_privilege():
            if not api(False, False, False):
                raise OSError('SetSuspendState failed')
    else:
        raise ValueError('Unsupported power action')


@contextlib.contextmanager
def _shutdown_privilege():
    class Luid(ctypes.Structure):
        _fields_ = [('low', ctypes.c_uint32), ('high', ctypes.c_int32)]
    class Privilege(ctypes.Structure):
        _fields_ = [('luid', Luid), ('attributes', ctypes.c_uint32)]
    class TokenPrivileges(ctypes.Structure):
        _fields_ = [('count', ctypes.c_uint32), ('privilege', Privilege)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel.GetCurrentProcess.argtypes, kernel.GetCurrentProcess.restype = [], ctypes.c_void_p
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [ctypes.c_void_p], ctypes.c_int
    advapi.OpenProcessToken.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
    advapi.OpenProcessToken.restype = ctypes.c_int
    advapi.LookupPrivilegeValueW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.POINTER(Luid)]
    advapi.LookupPrivilegeValueW.restype = ctypes.c_int
    advapi.AdjustTokenPrivileges.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(TokenPrivileges),
                                           ctypes.c_uint32, ctypes.POINTER(TokenPrivileges), ctypes.POINTER(ctypes.c_uint32)]
    advapi.AdjustTokenPrivileges.restype = ctypes.c_int
    token = ctypes.c_void_p()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x20 | 0x8, ctypes.byref(token)):
        raise OSError('Could not open this process shutdown privilege')
    previous, changed = TokenPrivileges(), False
    try:
        desired, returned = TokenPrivileges(), ctypes.c_uint32()
        desired.count, desired.privilege.attributes = 1, 2
        if not advapi.LookupPrivilegeValueW(None, 'SeShutdownPrivilege', ctypes.byref(desired.privilege.luid)):
            raise OSError('Could not resolve shutdown privilege')
        ctypes.set_last_error(0)
        if (not advapi.AdjustTokenPrivileges(token, False, ctypes.byref(desired), ctypes.sizeof(previous),
                                            ctypes.byref(previous), ctypes.byref(returned))
                or ctypes.get_last_error() != 0):
            raise OSError('Current user cannot enable sleep privilege')
        changed = True
        yield
    finally:
        try:
            if changed and not advapi.AdjustTokenPrivileges(token, False, ctypes.byref(previous), 0, None, None):
                raise OSError('Could not restore process shutdown privilege')
        finally:
            kernel.CloseHandle(token)


def abort_scheduled_shutdown(runner=None):
    (runner or _run_power_action)('abort_shutdown')
    return {'status': 'ABORTED', 'action': 'shutdown'}


class CompletionActions:
    def __init__(self):
        self._armed = None
        self._scheduled_nonce = None
        self._lock = threading.Lock()

    def disarm(self):
        with self._lock:
            self._armed = None

    def abort_scheduled_shutdown(self, runner=None):
        with self._lock:
            if self._scheduled_nonce is None:
                return {'status': 'NOT_SCHEDULED', 'action': 'shutdown'}
            result = abort_scheduled_shutdown(runner)
            self._scheduled_nonce = None
            return result

    def arm(self, action, batch_ids, confirmed=False, dispatch_nonce=None):
        if action not in ('none', 'exit', 'sleep', 'shutdown'):
            raise ValueError('Unknown completion action')
        if action == 'none':
            self.disarm()
            return {'armed': False, 'action': 'none', 'dispatch_nonce': None}
        if confirmed is not True:
            raise ValueError('Completion actions require explicit confirmation for this batch')
        identifiers = tuple(batch_ids)
        if (not identifiers or any(type(identifier) is not str or not identifier for identifier in identifiers)
                or len(set(identifiers)) != len(identifiers)):
            raise ValueError('Completion action requires unique actual batch IDs')
        nonce = dispatch_nonce or uuid.uuid4().hex
        if type(nonce) is not str or not nonce:
            raise ValueError('Invalid dispatch nonce')
        with self._lock:
            if self._scheduled_nonce is not None:
                raise RuntimeError('Cancel this session scheduled shutdown before arming another batch')
            self._armed = (action, identifiers, nonce)
        return {'armed': True, 'action': action, 'dispatch_nonce': nonce}

    def finish(self, batch_statuses, dispatch_nonce, runner=None, quit_callback=None,
               pending=False, active=False, maintenance=False):
        with self._lock:
            if self._armed is None:
                return {'status': 'DISARMED', 'action': 'none'}
            action, identifiers, nonce = self._armed
            if nonce != dispatch_nonce or not isinstance(batch_statuses, dict) or set(batch_statuses) != set(identifiers):
                self._armed = None
                return {'status': 'REFUSED', 'reason': 'batch_identity_changed', 'action': action}
            statuses = tuple(batch_statuses[identifier] for identifier in identifiers)
            if any(status in ('FAILED', 'CANCELLED', 'CANCELED', 'ERROR') for status in statuses):
                self._armed = None
                return {'status': 'DISARMED', 'reason': 'batch_failed_or_cancelled', 'action': action}
            if any(status != 'DONE' for status in statuses) or pending or active or maintenance:
                return {'status': 'WAITING', 'reason': 'work_still_active', 'action': action}
            # Consent is consumed before an external action, including failure;
            # subsequent polling can never repeat it automatically.
            self._armed = None
            if action == 'shutdown':
                # Scheduling and cancellation are one ordered critical section.
                (runner or _run_power_action)(action)
                self._scheduled_nonce = nonce
                return {'status': 'SCHEDULED', 'action': action, 'dispatch_nonce': nonce,
                        'shutdown_delay_seconds': 60}
        if action == 'exit':
            if quit_callback is None:
                return {'status': 'REFUSED', 'reason': 'quit_callback_missing', 'action': action}
            quit_callback()
        else:
            (runner or _run_power_action)(action)
        return {'status': 'EXECUTED', 'action': action, 'dispatch_nonce': nonce,
                'shutdown_delay_seconds': 60 if action == 'shutdown' else None}
