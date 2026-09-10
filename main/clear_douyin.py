import ctypes
import os
import sys
from ctypes import wintypes

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

from config import DOUYIN_DIR

# Windows Shell API
shell32 = ctypes.windll.shell32

FOF_ALLOWUNDO = 0x0040
FOF_NOCONFIRMATION = 0x0010
FOF_NOERRORUI = 0x0400
FOF_SILENT = 0x0004


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", wintypes.WORD),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", wintypes.LPVOID),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


FO_DELETE = 3


def send_to_recycle_bin(path: str) -> bool:
    path = os.path.abspath(path)
    if not os.path.exists(path):
        return False

    file_op = SHFILEOPSTRUCTW()
    file_op.hwnd = 0
    file_op.wFunc = FO_DELETE
    buf = ctypes.create_unicode_buffer(path + "\0")
    file_op.pFrom = ctypes.cast(buf, wintypes.LPCWSTR)
    file_op.pTo = None
    file_op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT
    file_op.fAnyOperationsAborted = False
    file_op.hNameMappings = None
    file_op.lpszProgressTitle = None

    result = shell32.SHFileOperationW(ctypes.byref(file_op))
    return result == 0


def clear_directory(target_dir: str) -> None:
    if not os.path.exists(target_dir):
        print(f"[INFO] 目录不存在，无需清理: {target_dir}")
        return

    if not os.path.isdir(target_dir):
        print(f"[ERROR] 路径不是一个目录: {target_dir}")
        sys.exit(1)

    print(f"[INFO] 开始清理目录（移至回收站）: {target_dir}")

    items = os.listdir(target_dir)
    if not items:
        print(f"[INFO] 目录为空，无需清理")
        return

    for item in items:
        item_path = os.path.join(target_dir, item)
        try:
            success = send_to_recycle_bin(item_path)
            if success:
                print(f"  [OK] 已移至回收站: {item}")
            else:
                print(f"  [ERROR] 移动失败: {item}")
        except Exception as e:
            print(f"  [ERROR] 移动失败: {item}, 原因: {e}")

    print(f"[INFO] 清理完成: {target_dir}")


if __name__ == "__main__":
    clear_directory(str(DOUYIN_DIR))