"""Two daisy-chained MAX7219 matrices: Firmata D6/D13/D10 on the LattePanda, or
Linux spidev (hardware SPI) on the Orange Pi 4 Pro; no servo."""
import os

SPI_SPEED_HZ = 1_000_000          # tested for hours with the 4 Pro's 3.3 V logic into a 5 V MAX7219
_SPI_IOC_WR_MODE = 0x40016B01     # _IOW('k', 1, u8)
_SPI_IOC_WR_MAX_SPEED_HZ = 0x40046B04  # _IOW('k', 4, u32)


class LedControl:
    def __init__(self, board):
        self.din = board.get_pin("d:6:o")
        self.clk = board.get_pin("d:13:o")
        self.cs = board.get_pin("d:10:o")
        self.din_state = None
        self.clk.write(0)
        self.cs.write(1)
        self.setup()

    def frame(self, device, opcode, value):
        """The bytes for one register write: the far device (1) is shifted out first."""
        return bytes(b for target in (1, 0) for b in ((opcode, value) if target == device else (0, 0)))

    def setup(self):
        self.rows = {}
        for device in (0, 1):
            self.transfer(device, 15, 0)  # Display-test mode off
            self.shutdown(device, True)
            self.transfer(device, 11, 7)  # Scan eight rows
            self.transfer(device, 9, 0)   # No numeric decoding
            for row in range(8):
                self.set_row(device, row, 0)

    def transfer(self, device, opcode, value):
        self.cs.write(0)
        for byte in self.frame(device, opcode, value):
            for bit in range(7, -1, -1):
                level = (byte >> bit) & 1
                if level != self.din_state:
                    self.din.write(level)
                    self.din_state = level
                self.clk.write(1)
                self.clk.write(0)
        self.cs.write(1)

    def set_row(self, device, row, value):
        if self.rows.get((device, row)) != value:
            self.transfer(device, row + 1, value)
            self.rows[device, row] = value

    def shutdown(self, device, off):
        self.transfer(device, 12, 0 if off else 1)

    def set_intensity(self, device, value):
        self.transfer(device, 10, max(0, min(15, value)))

    def draw_pair(self, left, right=None):
        right = left if right is None else right
        for row in range(8):
            # Current mounting from user's sketch: left=1, right=0, no flips.
            self.set_row(1, row, left[row])
            self.set_row(0, row, right[row])


class SpiLedControl(LedControl):
    """The same matrices on /dev/spidevN.M: each write() is one transfer with CS held low,
    mode 0, MSB first. Opens no other device; close() leaves the last face showing."""

    def __init__(self, path, speed_hz=SPI_SPEED_HZ, opener=os.open, ioctl=None):
        self.fd = opener(path, os.O_RDWR)
        try:
            if ioctl is None:
                import fcntl
                ioctl = fcntl.ioctl
            ioctl(self.fd, _SPI_IOC_WR_MODE, bytes([0]))
            ioctl(self.fd, _SPI_IOC_WR_MAX_SPEED_HZ, speed_hz.to_bytes(4, "little"))
            self.setup()
        except BaseException:
            os.close(self.fd)
            raise

    def transfer(self, device, opcode, value):
        os.write(self.fd, self.frame(device, opcode, value))

    def exit(self):
        os.close(self.fd)


def open_leds(port, board_factory=None):
    """(leds, close) for a spidev path or a Firmata serial port; a given board_factory
    (tests) is always used, so a test run on the robot never touches its SPI bus."""
    if board_factory is None and os.path.basename(port).startswith("spidev"):
        leds = SpiLedControl(port)
        return leds, leds.exit
    if board_factory is None:
        from pyfirmata2 import Arduino as board_factory
    board = board_factory(port)
    try:
        return LedControl(board), board.exit
    except BaseException:
        board.exit()
        raise
