"""Which Windows user opened a localhost TCP connection? (no admin rights needed)

client port -> owning process (GetExtendedTcpTable) -> session (ProcessIdToSessionId,
fallback WTSEnumerateProcesses) -> user name (WTSQuerySessionInformation).
On a multi-user (RDP) machine this identifies the person behind the browser
without any login. Returns None if the owner cannot be determined.
"""

from __future__ import annotations

import ctypes
import socket
from ctypes import wintypes

iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)

AF_INET = 2
TCP_TABLE_OWNER_PID_ALL = 5
WTSUserName = 5
WTSDomainName = 7


class _MIB_TCPROW_OWNER_PID(ctypes.Structure):
    _fields_ = [("dwState", wintypes.DWORD), ("dwLocalAddr", wintypes.DWORD), ("dwLocalPort", wintypes.DWORD),
                ("dwRemoteAddr", wintypes.DWORD), ("dwRemotePort", wintypes.DWORD), ("dwOwningPid", wintypes.DWORD)]


class _WTS_PROCESS_INFOW(ctypes.Structure):
    _fields_ = [("SessionId", wintypes.DWORD), ("ProcessId", wintypes.DWORD),
                ("pProcessName", wintypes.LPWSTR), ("pUserSid", ctypes.c_void_p)]


iphlpapi.GetExtendedTcpTable.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
                                         wintypes.ULONG, ctypes.c_int, wintypes.ULONG]
iphlpapi.GetExtendedTcpTable.restype = wintypes.DWORD
kernel32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
kernel32.ProcessIdToSessionId.restype = wintypes.BOOL
wtsapi32.WTSQuerySessionInformationW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_int,
                                                 ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.DWORD)]
wtsapi32.WTSQuerySessionInformationW.restype = wintypes.BOOL
wtsapi32.WTSEnumerateProcessesW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                            ctypes.POINTER(ctypes.POINTER(_WTS_PROCESS_INFOW)),
                                            ctypes.POINTER(wintypes.DWORD)]
wtsapi32.WTSEnumerateProcessesW.restype = wintypes.BOOL
wtsapi32.WTSFreeMemory.argtypes = [ctypes.c_void_p]


def _owner_pid(client_port: int, server_port: int) -> int | None:
    size = wintypes.DWORD(0)
    iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), False, AF_INET, TCP_TABLE_OWNER_PID_ALL, 0)
    for _ in range(3):
        buf = ctypes.create_string_buffer(size.value + 4096)
        size = wintypes.DWORD(len(buf))
        if iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), False, AF_INET, TCP_TABLE_OWNER_PID_ALL, 0) == 0:
            break
    else:
        return None
    n = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0]
    rows = ctypes.cast(ctypes.addressof(buf) + ctypes.sizeof(wintypes.DWORD),
                       ctypes.POINTER(_MIB_TCPROW_OWNER_PID * n)).contents
    for r in rows:
        lp = socket.ntohs(r.dwLocalPort & 0xFFFF)
        rp = socket.ntohs(r.dwRemotePort & 0xFFFF)
        if lp == client_port and rp == server_port:
            return int(r.dwOwningPid)
    return None


def _session_of(pid: int) -> int | None:
    sid = wintypes.DWORD()
    if kernel32.ProcessIdToSessionId(pid, ctypes.byref(sid)):
        return int(sid.value)
    p = ctypes.POINTER(_WTS_PROCESS_INFOW)()
    count = wintypes.DWORD()
    if not wtsapi32.WTSEnumerateProcessesW(None, 0, 1, ctypes.byref(p), ctypes.byref(count)):
        return None
    try:
        for i in range(count.value):
            if p[i].ProcessId == pid:
                return int(p[i].SessionId)
    finally:
        wtsapi32.WTSFreeMemory(p)
    return None


def _session_user(session_id: int) -> str | None:
    buf = wintypes.LPWSTR()
    n = wintypes.DWORD()
    if not wtsapi32.WTSQuerySessionInformationW(None, session_id, WTSUserName, ctypes.byref(buf), ctypes.byref(n)):
        return None
    try:
        return buf.value or None
    finally:
        wtsapi32.WTSFreeMemory(buf)


def connection_user(client_port: int, server_port: int) -> str | None:
    """Windows user name of the process owning the client end of a 127.0.0.1 connection."""
    try:
        pid = _owner_pid(client_port, server_port)
        if pid is None:
            return None
        sid = _session_of(pid)
        if sid is None:
            return None
        return _session_user(sid)
    except OSError:
        return None
