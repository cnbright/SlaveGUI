import ctypes
from ctypes import c_uint, c_byte, c_char_p, POINTER, c_ubyte, c_bool, wintypes, byref


class OperateCardLib:
    def __init__(self, dll_path: str):
        # Load the DLL
        self.dll = ctypes.CDLL(dll_path)

        # Define argument types for the DLL functions (based on header information)
        self._define_functions()

    def _define_functions(self):
        # Example: Define the argument types and return types for DLL functions
        self.dll.AMDCardInitial_DP.argtypes = []
        self.dll.AMDCardInitial_DP.restype = c_bool
        
        self.dll.EnumCard.argtypes = []
        self.dll.EnumCard.restype = POINTER(c_char_p)

        self.dll.GetCardInfo.argtypes = [POINTER(c_uint)]
        self.dll.GetCardInfo.restype = c_uint

        self.dll.SelectPort.argtypes = [c_uint]
        self.dll.SelectPort.restype = c_bool

        self.dll.Init.argtypes = [c_uint]
        self.dll.Init.restype = c_bool

        self.dll.WriteDPCD.argtypes = [c_uint, c_uint, POINTER(c_ubyte)]
        self.dll.WriteDPCD.restype = c_byte

        self.dll.ReadDPCD.argtypes = [c_uint, c_uint, POINTER(c_ubyte)]
        self.dll.ReadDPCD.restype = c_byte

        self.dll.IICWrite.argtypes = [c_ubyte, c_uint, c_uint, c_char_p]
        self.dll.IICWrite.restype = c_bool

        self.dll.IICRead.argtypes = [c_ubyte, c_uint, c_uint, c_char_p]
        self.dll.IICRead.restype = c_bool

         # BOOL IICWrite_S(unsigned char addr, unsigned char data);
        self.dll.IICWrite_S.argtypes = [c_ubyte, c_ubyte]
        self.dll.IICWrite_S.restype  = wintypes.BOOL

        # BOOL IICRead_S(unsigned char addr, char * value);
        # 用 POINTER(c_ubyte) 承接 char*
        self.dll.IICRead_S.argtypes = [c_ubyte, POINTER(c_ubyte)]
        self.dll.IICRead_S.restype  = wintypes.BOOL


        # Add other function definitions as needed...

    def enum_cards(self):
        """Enumerates all available cards"""
        cards = self.dll.EnumCard()
        return cards.contents

    def get_card_info(self):
        """Gets card info (CardNameLen)"""
        card_name_len = c_uint(0)
        card_info = self.dll.GetCardInfo(ctypes.byref(card_name_len))
        return card_info

    def select_port(self, port_id: int):
        """Select a specific port based on PortID"""
        return self.dll.SelectPort(c_uint(port_id))

    def init(self, card_id: int):
        """Initialize a card with the given CardID"""
        return self.dll.Init(c_uint(card_id))

    def write_dpcd(self, addr: int, data: bytes):
        length = len(data)
        if length == 0:
            raise ValueError("Data buffer cannot be empty.")

        data_pointer = (ctypes.c_ubyte * length)(*data)
        return self.dll.WriteDPCD(ctypes.c_uint(addr), ctypes.c_uint(length), data_pointer)

    def read_dpcd(self, addr: int, length: int):
        """Read from DPCD address"""
        data = (c_ubyte * length)()
        result = self.dll.ReadDPCD(c_uint(addr), c_uint(length), data)
        return bytes(data)

    def iic_write(self, addr: int, offset: int, data: bytes):
        """Write I2C data"""
        # return self.dll.IICWrite(c_ubyte(addr), c_uint(offset), c_uint(len(data)), data.encode())
        ok = self.dll.IICWrite(c_ubyte(addr), c_uint(offset), c_uint(len(data)), data)
        if not ok:
            payload = bytes(data)
            raise RuntimeError(
                f"IICWrite failed (addr=0x{addr:02X}, offset=0x{offset:02X}, len={len(payload)}, data={payload.hex(' ').upper()})"
            )
        return ok

    def iic_read(self, addr: int, offset: int, length: int):
        """Read I2C data"""
        buffer = ctypes.create_string_buffer(length)
        success = self.dll.IICRead(c_ubyte(addr), c_uint(offset), c_uint(length), buffer)
        if not success:
            raise RuntimeError(f"IICRead failed (addr=0x{addr:02X}, offset=0x{offset:02X}, len={length})")
        return buffer.raw
    
    # -------- 面向对象封装 --------
    def iic_write_s(self, addr: int, data: int) -> None:
        """
        向从设备 addr 写入单字节 data（无 offset）。
        失败时抛出 RuntimeError。
        """
        ok = self.dll.IICWrite_S(c_ubyte(addr & 0xFF), c_ubyte(data & 0xFF))
        if not ok:
            raise RuntimeError(f"IICWrite_S failed (addr=0x{addr:02X}, data=0x{data:02X})")

    def iic_read_s(self, addr: int) -> int:
        """
        从从设备 addr 读取单字节（无 offset），返回 0..255。
        失败时抛出 RuntimeError。
        """
        out_byte = c_ubyte(0)
        ok = self.dll.IICRead_S(c_ubyte(addr & 0xFF), byref(out_byte))
        if not ok:
            raise RuntimeError(f"IICRead_S failed (addr=0x{addr:02X})")
        return int(out_byte.value)


    def free_lib(self):
        """Free any allocated resources"""
        self.dll.FreeLib()

    def psr_enable(self):
        """Enable PSR"""
        self.dll.PSR_Enable()

    def psr_disable(self):
        """Disable PSR"""
        self.dll.PSR_Disable()

    def dpst_enable(self):
        """Enable DPST"""
        self.dll.DPST_Enable()

    def dpst_disable(self):
        """Disable DPST"""
        self.dll.DPST_Disable()

    def get_psr(self):
        """Get PSR status"""
        return self.dll.Get_PSR()

    def get_dpst(self):
        """Get DPST status"""
        return self.dll.Get_DPST()

    def get_display_config(self):
        """Get the display configuration"""
        self.dll.getdisplayconfig()

    def get_card_type(self):
        """
        读取 DLL 中的全局变量 Card_Type
        """
        var = c_byte.in_dll(self.dll, "Card_Type")
        return var.value
    
    def parse_dpcd(self, read_data: bytes):
        """
        解析 DPCD 的前 16 字节并返回详细含义
        """

        if len(read_data) < 16:
            raise ValueError("Insufficient DPCD data. Expected at least 16 bytes.")

        read_data = list(list(read_data))

        # 获取eDP版本
        if read_data[0] == 0x10:
            edp_version = "1.0"
        elif read_data[0] == 0x11:
            edp_version = "1.1"
        elif read_data[0] == 0x12:
            edp_version = "1.2"
        elif read_data[0] == 0x13:
            edp_version = "1.3"
        elif read_data[0] == 0x14:
            edp_version = "1.4"
        else:
            edp_version = "Unknown"

        # 获取单lane速率
        if read_data[1] == 0x06:
            link_rate = "1.62Gbps/lane"
        elif read_data[1] == 0x0A:
            link_rate = "2.7Gbps/lane"
        elif read_data[1] == 0x14:
            link_rate = "5.4Gbps/lane"
        elif read_data[1] == 0x1E:
            link_rate = "8.1Gbps/lane"
        else:
            link_rate = "Unknown"

        # 获取lane数
        if read_data[2]&0x0F == 0x01:
            link_count = "1-lane"
        elif read_data[2]&0x0F == 0x02:
            link_count = "2-lane"
        elif read_data[2]&0x0F == 0x04:
            link_count = "4-lane"
        else:
            link_count = "Unknown"

        # 返回解析的结果
        dpcd_info = {
            "DPCD v.": edp_version,
            "Max Link Rate": link_rate,
            "Max Lane Count": link_count
        }

        return dpcd_info


'''
——————————————————————————————————————————————————————————
|               Parade TC3410 TCON AXU code              |
——————————————————————————————————————————————————————————
'''

# 兼容PARADE TC3232 TC3410
class Parade_TC3410(OperateCardLib):
    # reg_val要求输入数组
    def iic_over_aux_write(self, slave_addr, reg_addr, reg_val: list):
        self.write_dpcd(0x00480, b"\x50")
        self.write_dpcd(0x00480, b"\x41")
        self.write_dpcd(0x00480, b"\x52")
        self.write_dpcd(0x00480, b"\x41")
        self.write_dpcd(0x00480, b"\x44")
        self.write_dpcd(0x00480, b"\x45")
        self.write_dpcd(0x00480, b"\x2D")
        self.write_dpcd(0x00480, b"\x46")
        self.write_dpcd(0x00480, b"\x57")
        self.write_dpcd(0x00480, b"\x2D")
        self.write_dpcd(0x00480, b"\x44")
        self.write_dpcd(0x00480, b"\x50")
        self.write_dpcd(0x00480, b"\x00")
        self.write_dpcd(0x00480, b"\x06")
        self.write_dpcd(0x00480, b"\x03")
        self.write_dpcd(0x00480, b"\x03")

        base = self.read_dpcd(0x00480, 1)
        # print("DPCD[00000..0000F] =", base.hex(" ").upper())

        self.write_dpcd(0x00482, b"\x80")
        self.write_dpcd(0x0048B, b"\x90")
        # 启用slave addr 采用8bit地址
        # self.write_dpcd(0x0048E, b"\x47")
        self.write_dpcd(0x0048E, bytes([slave_addr]))

        # slave addr采用7bit地址
        self.iic_write(slave_addr>>1, reg_addr, bytes(reg_val))

        self.write_dpcd(0x00480, b"\x00")
        self.write_dpcd(0x00480, b"\x00")

    def iic_over_aux_read(self, slave_addr, reg_addr,lenth):
        self.write_dpcd(0x00480, b"\x50")
        self.write_dpcd(0x00480, b"\x41")
        self.write_dpcd(0x00480, b"\x52")
        self.write_dpcd(0x00480, b"\x41")
        self.write_dpcd(0x00480, b"\x44")
        self.write_dpcd(0x00480, b"\x45")
        self.write_dpcd(0x00480, b"\x2D")
        self.write_dpcd(0x00480, b"\x46")
        self.write_dpcd(0x00480, b"\x57")
        self.write_dpcd(0x00480, b"\x2D")
        self.write_dpcd(0x00480, b"\x44")
        self.write_dpcd(0x00480, b"\x50")
        self.write_dpcd(0x00480, b"\x00")
        self.write_dpcd(0x00480, b"\x06")
        self.write_dpcd(0x00480, b"\x03")
        self.write_dpcd(0x00480, b"\x03")

        base = self.read_dpcd(0x00480, 1)
        # print("DPCD[00000..0000F] =", base.hex(" ").upper())

        self.write_dpcd(0x00482, b"\x80")
        self.write_dpcd(0x0048B, b"\x90")
        # 启用slave addr 采用8bit地址
        # self.write_dpcd(0x0048E, b"\x47")
        self.write_dpcd(0x0048E, bytes([slave_addr]))

        # slave addr采用7bit地址
        reasult = self.iic_read(slave_addr>>1, reg_addr, lenth)

        self.write_dpcd(0x00480, b"\x00")
        self.write_dpcd(0x00480, b"\x00")
        return reasult


    # 最低位=0表示MTP,1 DAC, 
    def write_vcom(self, reg_val):
        self.write_dpcd(0x00480, b"\x50")
        self.write_dpcd(0x00480, b"\x41")
        self.write_dpcd(0x00480, b"\x52")
        self.write_dpcd(0x00480, b"\x41")
        self.write_dpcd(0x00480, b"\x44")
        self.write_dpcd(0x00480, b"\x45")
        self.write_dpcd(0x00480, b"\x2D")
        self.write_dpcd(0x00480, b"\x46")
        self.write_dpcd(0x00480, b"\x57")
        self.write_dpcd(0x00480, b"\x2D")
        self.write_dpcd(0x00480, b"\x44")
        self.write_dpcd(0x00480, b"\x50")
        self.write_dpcd(0x00480, b"\x00")
        self.write_dpcd(0x00480, b"\x06")
        self.write_dpcd(0x00480, b"\x03")
        self.write_dpcd(0x00480, b"\x03")

        base = self.read_dpcd(0x00480, 1)
        # print("DPCD[00000..0000F] =", base.hex(" ").upper())

        self.write_dpcd(0x00482, b"\x80")
        self.write_dpcd(0x0048B, b"\x90")
        self.write_dpcd(0x0048E, b"\x9F") # IIC addr

        # 0x03写入0x28,0x04写入0x14
        self.iic_write_s(0x4F,reg_val)

        self.write_dpcd(0x00480, b"\x00")
        self.write_dpcd(0x00480, b"\x00")

    # 最低位=0表示MTP,1 DAC, 
    def read_vcom(self):
        self.write_dpcd(0x00480, b"\x50")
        self.write_dpcd(0x00480, b"\x41")
        self.write_dpcd(0x00480, b"\x52")
        self.write_dpcd(0x00480, b"\x41")
        self.write_dpcd(0x00480, b"\x44")
        self.write_dpcd(0x00480, b"\x45")
        self.write_dpcd(0x00480, b"\x2D")
        self.write_dpcd(0x00480, b"\x46")
        self.write_dpcd(0x00480, b"\x57")
        self.write_dpcd(0x00480, b"\x2D")
        self.write_dpcd(0x00480, b"\x44")
        self.write_dpcd(0x00480, b"\x50")
        self.write_dpcd(0x00480, b"\x00")
        self.write_dpcd(0x00480, b"\x06")
        self.write_dpcd(0x00480, b"\x03")
        self.write_dpcd(0x00480, b"\x03")

        base = self.read_dpcd(0x00480, 1)
        # print("DPCD[00000..0000F] =", base.hex(" ").upper())

        self.write_dpcd(0x00482, b"\x80")
        self.write_dpcd(0x0048B, b"\x90")
        self.write_dpcd(0x0048E, b"\x9F") # IIC addr

        # 0x03写入0x28,0x04写入0x14
        result = self.iic_read_s(0x4F)

        self.write_dpcd(0x00480, b"\x00")
        self.write_dpcd(0x00480, b"\x00")

        return result


    # 用于修改tcon reg
    def tc3410_reg_write(self, tc3410_page, tc3410_reg, reg_val):
        self.write_dpcd(0x00490, b"\x00")
        self.write_dpcd(0x00490, b"\x50")
        self.write_dpcd(0x00490, b"\x41")
        self.write_dpcd(0x00490, b"\x52")
        self.write_dpcd(0x00490, b"\x41")
        self.write_dpcd(0x00490, b"\x41")
        self.write_dpcd(0x00490, b"\x55")
        self.write_dpcd(0x00490, b"\x58")
        self.write_dpcd(0x00490, b"\x2D")
        self.write_dpcd(0x00490, b"\x52")
        self.write_dpcd(0x00490, b"\x45")
        self.write_dpcd(0x00490, b"\x47")

        base = self.read_dpcd(0x00490, 1)
        #print("DPCD[00000..0000F] =", base.hex(" ").upper())

        self.write_dpcd(0x00491, bytes([tc3410_page]))
        self.write_dpcd(0x00492, bytes([tc3410_reg]))# 6d 6C为两个L0寄存器
        self.write_dpcd(0x00493, bytes([reg_val]))# 寄存器值
        self.write_dpcd(0x00490, b"\x00")


    def tc3410_reg_read(self, tc3410_page, tc3410_reg):
        self.write_dpcd(0x00490, b"\x00")
        self.write_dpcd(0x00490, b"\x50")
        self.write_dpcd(0x00490, b"\x41")
        self.write_dpcd(0x00490, b"\x52")
        self.write_dpcd(0x00490, b"\x41")
        self.write_dpcd(0x00490, b"\x41")
        self.write_dpcd(0x00490, b"\x55")
        self.write_dpcd(0x00490, b"\x58")
        self.write_dpcd(0x00490, b"\x2D")
        self.write_dpcd(0x00490, b"\x52")
        self.write_dpcd(0x00490, b"\x45")
        self.write_dpcd(0x00490, b"\x47")

        base = self.read_dpcd(0x00490, 1)
        #print("DPCD[00000..0000F] =", base.hex(" ").upper())

        self.write_dpcd(0x00491, bytes([tc3410_page]))
        self.write_dpcd(0x00492, bytes([tc3410_reg]))# 6d 6C为两个L0寄存器
        result = self.read_dpcd(0x00493, 1)# 寄存器值
        self.write_dpcd(0x00490, b"\x00")
        return result


    def change_gamma(self, tc3410_reg, reg_val):
        self.tc3410_reg_write(0x04, tc3410_reg, reg_val)

        # 下面用于生效gamma reg变化
        self.tc3410_reg_write(0x04, 0xf0, 0x00)
        self.tc3410_reg_write(0x04, 0xf2, 0x00)
        self.tc3410_reg_write(0x04, 0xf0, 0x80)
        self.tc3410_reg_write(0x04, 0xf0, 0x00)
        self.tc3410_reg_write(0x04, 0xf0, 0x01)
        self.tc3410_reg_write(0x04, 0xf0, 0x81)
        self.tc3410_reg_write(0x04, 0xf0, 0x00)
        self.tc3410_reg_write(0x04, 0xf0, 0x82)
        self.tc3410_reg_write(0x04, 0xf0, 0x02)
        self.tc3410_reg_write(0x04, 0xf0, 0x82)
        self.tc3410_reg_write(0x04, 0xf0, 0x00)


'''
——————————————————————————————————————————————————————————
|                   NT71877 TCON AXU code                |
——————————————————————————————————————————————————————————
'''
# 兼容NT71877 71866
class Nova_NT71877(OperateCardLib):
    def iic_over_aux_write(self, slave_addr, reg_addr, reg_val:list):
        # 启动TCON IIC OVER AUX功能
        self.write_dpcd(0x00102, b'\xC0')
        self.iic_write(0x60, 0x02, b"\x04\x00")
        # 采用7bit slave addr
        # self.iic_write(0x23, 0x03, b"\x01")
        # PMIC SLAVe 7bit地址0x23  8bit 0x46,自动8bit转7bit
        self.iic_write(slave_addr>>1, reg_addr, bytes(reg_val))

    def iic_over_aux_read(self, slave_addr, reg_addr, len):
        # 启动TCON IIC OVER AUX功能
        self.write_dpcd(0x00102, b'\xC0')
        self.iic_write(0x60, 0x02, b"\x04\x00")
        # 采用7bit slave addr
        # self.iic_write(0x23, 0x03, b"\x01")
        # PMIC SLAVe 7bit地址0x23  8bit 0x46,自动8bit转7bit
        return self.iic_read(slave_addr>>1, reg_addr, len)


    def write_vcom(self, reg_val):
        # 启动TCON IIC OVER AUX功能
        self.write_dpcd(0x00102, b'\xC0')
        self.iic_write(0x60, 0x02, b"\x04\x00")
        # 写入VCOM，VCOM同PARADE
        self.iic_write_s(0x4F, reg_val)


    def read_vcom(self):
        # 启动TCON IIC OVER AUX功能
        self.write_dpcd(0x00102, b'\xC0')
        self.iic_write(0x60, 0x02, b"\x04\x00")
        # 写入VCOM，VCOM同PARADE
        return self.iic_read_s(0x4F)   


    def change_gamma(self, gamma_reg, reg_val):
        # 启动TCON IIC OVER AUX功能
        self.write_dpcd(0x00102, b'\xC0')
        self.iic_write(0x60, 0x02, b"\x04\x00")
        # 采用7bit slave addr
        # self.iic_write(0x23, 0x03, b"\x01")
        # TCON Reg地址 7bit地址0x61  8bit 0xC2
        self.iic_write(0xC2>>1, 0x34, bytes([gamma_reg, reg_val]))


    # NOVA TCON，采用page——reg_addr——reg_val形式组织reg，使用IIC操作，不能单纯通过DPCD控制
    def write_tcon_reg(self, page, reg_addr, reg_val):
        # 启动TCON IIC OVER AUX功能
        self.write_dpcd(0x00102, b'\xC0')
        self.iic_write(0x60, 0x02, b"\x04\x00")
        # 采用7bit slave addr
        # self.iic_write(0x23, 0x03, b"\x01")
        # TCON Reg地址 7bit地址0x61  8bit 0xC2
        self.iic_write(0xC0>>1, page, bytes([reg_addr, reg_val]))

    # NOVA TCON一次读取一页，从每一页的0x01开始读取
    def read_tcon_reg(self, page, reg_addr):
        # 启动TCON IIC OVER AUX功能
        self.write_dpcd(0x00102, b'\xC0')
        self.iic_write(0x60, 0x02, b"\x04\x00")
        # 采用7bit slave addr
        # self.iic_write(0x23, 0x03, b"\x01")
        # TCON Reg地址 7bit地址0x61  8bit 0xC2
        self.iic_write(0xC2>>1, page, bytes([0x00]))
        result = self.iic_read(0xC2>>1, page, 254)

        return [hex(i) for i in list(result)][reg_addr-1]





'''
——————————————————————————————————————————————————————————
|                   ANX TCON AXU code                    |
——————————————————————————————————————————————————————————
'''
class ANX_ANX2176(OperateCardLib):
    def iic_over_aux_write(self, slave_addr, reg_addr, reg_val:list):
        # TCON Can be accessed after writing password
        self.write_dpcd(0x004F5, b"\x41\x56\x4F\x20\x16")
        self.write_dpcd(0x004F0, b"\x0E\x00\x00\x00")
        #card_lib.read_dpcd(0x004F4, 1)
        self.write_dpcd(0x004F3, b"\x01")
        #card_lib.read_dpcd(0x004F4, 1)
        self.write_dpcd(0x004F0, b"\x0E\x00\x00\x30\x09")
        # IIC
        self.iic_write(slave_addr>>1, reg_addr, bytes(reg_val))

    def iic_over_aux_read(self, slave_addr, reg_addr, length):
        # TCON Can be accessed after writing password
        self.write_dpcd(0x004F5, b"\x41\x56\x4F\x20\x16")
        self.write_dpcd(0x004F0, b"\x0E\x00\x00\x00")
        #card_lib.read_dpcd(0x004F4, 1)
        self.write_dpcd(0x004F3, b"\x01")
        #card_lib.read_dpcd(0x004F4, 1)
        self.write_dpcd(0x004F0, b"\x0E\x00\x00\x30\x09")
        # IIC
        return self.iic_read(slave_addr>>1, reg_addr, length)


    def write_vcom(self, reg_val):
        self.write_dpcd(0x004F5, b"\x41\x56\x4F\x20\x16")
        self.write_dpcd(0x004F0, b"\x0E\x00\x00\x00")
        #card_lib.read_dpcd(0x004F4, 1)
        self.write_dpcd(0x004F3, b"\x01")
        #card_lib.read_dpcd(0x004F4, 1)
        self.write_dpcd(0x004F0, b"\x0E\x00\x00\x30\x09")
        
        # IIC
        self.iic_write_s(0x4F, reg_val)

    def read_vcom(self):
        self.write_dpcd(0x004F5, b"\x41\x56\x4F\x20\x16")
        self.write_dpcd(0x004F0, b"\x0E\x00\x00\x00")
        #card_lib.read_dpcd(0x004F4, 1)
        self.write_dpcd(0x004F3, b"\x01")
        #card_lib.read_dpcd(0x004F4, 1)
        self.write_dpcd(0x004F0, b"\x0E\x00\x00\x30\x09")
        # IIC
        return self.iic_read_s(0x4F)


    def change_gamma(self, gamma_reg, reg_val):
        # TCON Can be accessed after writing password
        self.write_dpcd(0x004F5, b"\x41\x56\x4F\x20\x16")
        self.write_dpcd(0x004F0, b"\x0E\x00\x00\x00")
        #card_lib.read_dpcd(0x004F4, 1)
        self.write_dpcd(0x004F3, b"\x01")

        self.write_dpcd(0x004F0, b"\x0A\x00\x00"+ bytes([gamma_reg,reg_val]))
        

    def get_all_gamma(self):
        # todo，获取所有Gamma
        pass


    # ANX TCON REG按照OSB ID——OFFSET——VALUE结构组织REG
    def write_tcon_reg(self, osb, offset, reg_val):
        # 输入16进制按字节拆分，转为两字节byte
        b_offset = offset.to_bytes(2, 'big')   # 2个字节，大端序

        # TCON Can be accessed after writing password
        self.write_dpcd(0x004F5, b"\x41\x56\x4F\x20\x16")
        self.write_dpcd(0x004F0, b"\x0E\x00\x00\x00")
        #card_lib.read_dpcd(0x004F4, 1)
        self.write_dpcd(0x004F3, b"\x01")
        
        # 目前猜测适用的格式为 osb(1byte) + 0x00 + offset(2bytes) + reg_val(1byte)
        self.write_dpcd(0x004F0, bytes([osb,0x00])+b_offset+bytes([reg_val]))

    # ANX TCON REG按照OSB ID——OFFSET——VALUE结构组织REG
    def read_tcon_reg(self, osb, offset, len):
        # 输入16进制按字节拆分，转为两字节byte
        b_offset = offset.to_bytes(2, 'big')   # 2个字节，大端序

        # TCON Can be accessed after writing password
        self.write_dpcd(0x004F5, b"\x41\x56\x4F\x20\x16")
        self.write_dpcd(0x004F0, b"\x0E\x00\x00\x00")
        #card_lib.read_dpcd(0x004F4, 1)
        self.write_dpcd(0x004F3, b"\x01")

        self.write_dpcd(0x004F0,bytes([osb,0x00])+b_offset)

        return int.from_bytes(self.read_dpcd(0x004F4, len), byteorder='big')
