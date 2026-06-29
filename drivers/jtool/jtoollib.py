# v1.1 增加no reg

from ctypes import (
    cdll, c_void_p,
    c_uint8, c_uint16, c_uint32,
    c_char_p, c_int,
    POINTER, CFUNCTYPE, byref
)

from enum import IntEnum
import ctypes

import time
import re


# 加载dll
jtool = cdll.LoadLibrary("./jtool.dll")


class DevTypeEnum(IntEnum):
    dev_all = -1
    dev_i2c = 0
    dev_io = 1
    dev_spi = 2
    dev_can = 3
    dev_max = 4

class ErrorType(IntEnum):
    ErrNone       = 0           # 成功
    ErrParam      = 1 << 0      # 参数错误
    ErrDisconnect = 1 << 1      # USB 断开
    ErrBusy       = 1 << 2      # USB 发送忙
    ErrWaiting    = 1 << 3      # 正在等待回复
    ErrTimeOut    = 1 << 4      # 通信超时
    ErrDataParse  = 1 << 5      # 通信数据错误
    ErrFailACK    = 1 << 6      # 返回失败参数

class REGADDR_TYPE(IntEnum):
    REGADDR_NONE  = 0  # 不发送地址
    REGADDR_8Bit  = 1  # 发送8位地址
    REGADDR_16Bit = 2  # 发送16位地址
    REGADDR_24Bit = 3  # 发送24位地址
    REGADDR_32Bit = 4  # 发送32位地址

class QSPI_TYPE(IntEnum):
    SINGLEALL = 0  # 所有阶段都是单线
    QUADALL   = 1  # 所有阶段都是四线
    QUADDATA  = 2  # 仅数据阶段四线，其他单线
    SINGLECMD = 3  # 仅指令阶段单线，其他四线

class SPICK_TYPE(IntEnum):
    LOW_1EDG   = 0
    LOW_2EDG   = 1
    HIGH_1EDG  = 2
    HIGH_2EDG  = 3

class SPIFIRSTBIT_TYPE(IntEnum):
    ENDIAN_MSB = 0  # 高位在前
    ENDIAN_LSB = 1  # 低位在前

class FIELDLEN_TYPE(IntEnum):
    FIELD_NONE  = 0  # 无
    FIELD_ONE   = 1  # 1 字节
    FIELD_TWO   = 2  # 2 字节
    FIELD_THREE = 3  # 3 字节
    FIELD_FOUR  = 4  # 4 字节

class INT_TYPE(IntEnum):
    INT_NONE       = 0  # 无
    INT_RISE       = 1  # 上升沿
    INT_FALL       = 2  # 下降沿
    INT_HIGH       = 3  # 高电平
    INT_LOW        = 4  # 低电平
    INT_RISE_FALL  = 5  # 双边沿

# (JI2C、JSPI) INT 引脚中断回调函数类型
I2CIntCallbackFun = ctypes.CFUNCTYPE(None)
SPIIntCallbackFun = ctypes.CFUNCTYPE(None)



# I2CScan(void* DevHandle, uint8_t* cnt, uint8_t* result);
jtool.I2CScan.argtypes  = [c_void_p, POINTER(c_uint8), POINTER(c_uint8)]
jtool.I2CScan.restype   = ErrorType

jtool.I2CScan.argtypes = [c_void_p, POINTER(c_uint8), POINTER(c_uint8)]
jtool.I2CScan.restype  = c_int  # ErrorType

# I2CWrite(void* DevHandle, uint8_t slave_addr,
#          REGADDR_TYPE reg_type, uint32_t reg_addr,
#          uint16_t len, uint8_t* data);
jtool.I2CWrite.argtypes = [
    c_void_p, c_uint8,
    c_int,       # REGADDR_TYPE
    c_uint32,    # reg_addr
    c_uint16,    # len
    POINTER(c_uint8)
]
jtool.I2CWrite.restype  = ErrorType

# I2CRead(void* DevHandle, uint8_t slave_addr,
#         REGADDR_TYPE reg_type, uint32_t reg_addr,
#         uint16_t len, uint8_t* buf);
jtool.I2CRead.argtypes  = [
    c_void_p, c_uint8,
    c_int,       # REGADDR_TYPE
    c_uint32,
    c_uint16,
    POINTER(c_uint8)
]
jtool.I2CRead.restype   = ErrorType

# I2CReadWithDelay(void* DevHandle, uint8_t slave_addr,
#                  REGADDR_TYPE reg_type, uint32_t reg_addr,
#                  uint16_t len, uint8_t* buf,
#                  uint8_t sr_delay, uint8_t raddr_delay);
jtool.I2CReadWithDelay.argtypes = [
    c_void_p, c_uint8,
    c_int,       # REGADDR_TYPE
    c_uint32,
    c_uint16,
    POINTER(c_uint8),
    c_uint8,     # sr_delay
    c_uint8      # raddr_delay
]
jtool.I2CReadWithDelay.restype  = ErrorType

# EEWrite(void* DevHandle, uint8_t base_slave_addr,
#         REGADDR_TYPE reg_type, uint16_t page_size,
#         uint32_t reg_addr, uint32_t len,
#         uint8_t* data);
jtool.EEWrite.argtypes = [
    c_void_p, c_uint8,
    c_int,       # REGADDR_TYPE
    c_uint16,    # page_size
    c_uint32,    # reg_addr
    c_uint32,    # len
    POINTER(c_uint8)
]
jtool.EEWrite.restype  = ErrorType

# EERead(void* DevHandle, uint8_t base_slave_addr,
#        REGADDR_TYPE reg_type, uint32_t reg_addr,
#        uint32_t len, uint8_t* buf);
jtool.EERead.argtypes = [
    c_void_p, c_uint8,
    c_int,       # REGADDR_TYPE
    c_uint32,    # reg_addr
    c_uint32,    # len
    POINTER(c_uint8)
]
jtool.EERead.restype  = ErrorType

# 回调函数类型： typedef void (*I2CIntCallbackFun)(void);
I2CIntCallbackFun = CFUNCTYPE(None)

# I2CRegisterIntCallback(void* DevHandle, INT_TYPE inttype, I2CIntCallbackFun callback);
# 注意：INT_TYPE 也需按头文件映射为 c_int 或同等类型
INT_TYPE = c_int
jtool.I2CRegisterIntCallback.argtypes = [
    c_void_p,
    INT_TYPE,
    I2CIntCallbackFun
]
jtool.I2CRegisterIntCallback.restype  = ErrorType

# I2CCloseIntCallback(void* DevHandle);
jtool.I2CCloseIntCallback.argtypes = [c_void_p]
jtool.I2CCloseIntCallback.restype  = ErrorType

# JI2CReboot(void* DevHandle);
jtool.JI2CReboot.argtypes = [c_void_p]
jtool.JI2CReboot.restype  = ErrorType

# JI2CSetVcc(void* DevHandle, uint8_t val);
jtool.JI2CSetVcc.argtypes = [c_void_p, c_uint8]
jtool.JI2CSetVcc.restype  = ErrorType

# JI2CSetVio(void* DevHandle, uint8_t val);
jtool.JI2CSetVio.argtypes = [c_void_p, c_uint8]
jtool.JI2CSetVio.restype  = ErrorType

# JI2CSetSpeed(void* DevHandle, uint8_t val);
jtool.JI2CSetSpeed.argtypes = [c_void_p, c_uint8]
jtool.JI2CSetSpeed.restype  = ErrorType

# JI2CSetHardVersion(void* DevHandle, char* version);
jtool.JI2CSetHardVersion.argtypes = [c_void_p, c_char_p]
jtool.JI2CSetHardVersion.restype  = ErrorType

# JI2CSetID(void* DevHandle, uint16_t val);
jtool.JI2CSetID.argtypes = [c_void_p, c_uint16]
jtool.JI2CSetID.restype  = ErrorType

# JI2CIntoBoot(void* DevHandle);
jtool.JI2CIntoBoot.argtypes = [c_void_p]
jtool.JI2CIntoBoot.restype  = ErrorType



# 2) 映射枚举常量（可根据 jtool.h 补充）
DEV_ALL   = -1
DEV_I2C   = 0

# 3) 定义函数原型
#    char* DevicesScan(int DevType, int* OutCnt);
jtool.DevicesScan.argtypes = [c_int, POINTER(c_int)]
jtool.DevicesScan.restype  = c_char_p

#    void* DevOpen(int DevType, char* Sn, int Id);
jtool.DevOpen.argtypes    = [c_int, c_char_p, c_int]
jtool.DevOpen.restype     = c_void_p

#    BOOL DevClose(void* DevHandle);
jtool.DevClose.argtypes   = [c_void_p]
jtool.DevClose.restype    = c_int   # BOOL == int

#    ErrorType I2CRead(void*, uint8_t, REGADDR_TYPE, uint32_t, uint16_t, uint8_t*);
jtool.I2CRead.argtypes    = [
    c_void_p, c_uint8, c_int,
    c_uint32, c_uint16,
    POINTER(c_uint8)
]
jtool.I2CRead.restype     = c_int   # ErrorType == int




# … 如有更多函数，依此类推 …
# 4) 调用示例
def scan_devices_sn():
    # 调用 DLL 扫描设备
    cnt = c_int()
    raw = jtool.DevicesScan(DEV_ALL, byref(cnt))
    # 拆成每行字符串
    lines = raw.decode('utf-8').split('\r\n') if raw else []

    sn_list = []
    for line in lines:
        # 匹配 “SN:...” 中的内容
        m = re.search(r"SN:([0-9A-Za-z]+)", line)
        if m:
            sn_list.append(m.group(1))
    return sn_list

def open_device(sn: str = None, idx: int = -1):
    handle = jtool.DevOpen(DEV_I2C, sn.encode('ascii') if sn else None, idx)
    if not handle:
        raise RuntimeError("打开设备失败")
    return handle



def i2c_write(handle, addr, reg, data):
    """
    向 I2C 从机写入数据。

    参数：
      handle:   设备句柄，由 DevOpen 返回
      addr:     7-bit 从机地址（0x00–0x7F）
      reg:      寄存器地址（0x00–0xFF）
      data:     可迭代对象，元素为 0–255 的整数，或 bytes

    抛出：
      RuntimeError 如果写入失败，会带上错误码。
    """
    # 如果传入 bytes 或 bytearray，转成 list
    if isinstance(data, (bytes, bytearray)):
        data_list = list(data)
    else:
        data_list = list(data)

    length = len(data_list)
    if length == 0:
        raise ValueError("data 不能为空")

    # 构造 C 风格缓冲区
    buf = (c_uint8 * length)(*data_list)

    # 调用底层 I2CWrite
    err = jtool.I2CWrite(
        handle,
        c_uint8(addr),
        c_int(REGADDR_TYPE.REGADDR_8Bit),
        c_uint32(reg),
        c_uint16(length),
        buf
    )
    if err != 0:
        raise RuntimeError(f"I2CWrite 失败，错误码：{err}")


def i2c_write_no_reg(handle, addr, data):
    """
    向 I2C 从机写入数据。

    参数：
      handle:   设备句柄，由 DevOpen 返回
      addr:     7-bit 从机地址（0x00–0x7F）
      reg:      寄存器地址（0x00–0xFF）
      data:     可迭代对象，元素为 0–255 的整数，或 bytes

    抛出：
      RuntimeError 如果写入失败，会带上错误码。
    """
    # 如果传入 bytes 或 bytearray，转成 list
    if isinstance(data, (bytes, bytearray)):
        data_list = list(data)
    else:
        data_list = list(data)

    length = len(data_list)
    if length == 0:
        raise ValueError("data 不能为空")

    # 构造 C 风格缓冲区
    buf = (c_uint8 * length)(*data_list)

    # 调用底层 I2CWrite
    err = jtool.I2CWrite(
        handle,
        c_uint8(addr),
        c_int(REGADDR_TYPE.REGADDR_NONE),
        c_uint32(0),
        c_uint16(length),
        buf
    )
    if err != 0:
        raise RuntimeError(f"I2CWrite 失败，错误码：{err}")

# # 使用示例
# if __name__ == "__main__":
#     # 假设已经 open 并获得 handle
#     # handle = jtool.DevOpen(DEV_I2C, None, 0)

#     # 写入示例：向 0x48 从机的寄存器 0x10 写 [0x00,0x01,0x02,0x0F,0x0E]
#     handle = open_device(devices[0])
#     try:
#         i2c_write(handle=handle, addr=0x90, reg=0x10, data=[0x00,0x01,0x02,0x0F,0x0E])
#     finally:
#         jtool.DevClose(handle)


def i2c_read(handle, addr, reg, length=1):
    """
    从 I²C 设备读取数据。

    参数:
      handle: I²C 设备句柄
      addr:   7-bit 从机地址 (0x00–0x7F)
      reg:    寄存器地址 (0x00–0xFF)
      length: 要读取的字节数 (默认为 1)

    返回:
      一个长度为 length 的整数列表，元素范围 0–255

    抛出:
      RuntimeError 如果读取失败，会带上错误码。
    """
    if length <= 0:
        raise ValueError("length 必须大于 0")

    # 构造 C 风格缓冲区
    buf = (c_uint8 * length)()

    # 调用底层 I2CRead
    err = jtool.I2CRead(
        handle,
        c_uint8(addr),
        c_int(REGADDR_TYPE.REGADDR_8Bit),
        c_uint32(reg),
        c_uint16(length),
        buf
    )
    if err != 0:
        raise RuntimeError(f"I2CRead 失败，错误码：{err}")

    # buf 现在包含了读取到的字节
    return [buf[i] for i in range(length)]


# # —— 使用示例 ——
# if __name__ == "__main__":

#     handle = open_device(devices[0])
#     try:
#         # 假设 handle 已通过 DevOpen 得到
#         # 读取寄存器 0x10 的 1 个字节
#         byte0 = i2c_read(handle, addr=0x90, reg=0x10)[0]
#         print(f"寄存器 0x10 的值: 0x{byte0:02X}")

#         # 如果想一次读 5 个字节
#         data5 = i2c_read(handle, addr=0x90, reg=0x10, length=5)
#         print("连续 5 字节数据:", [f"0x{b:02X}" for b in data5])
#     finally:
#         jtool.DevClose(handle)
