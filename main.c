/*******************************************************************************
 * Cable Jig Tester Firmware v4.10
 * IDE      : MPLAB X v6.30
 * Compiler : XC16 v2.10
 * MCU      : dsPIC30F4011  (2KB RAM, 48KB Flash)
 * Clock    : 7.3728 MHz XT + PLL x8 -> Fosc = 58.9824 MHz, Fcy = Fosc/4 = 14.7456 MHz
 *
 * PCA9555 mapping (from schematic):
 *   U19 (0x20) = A-side J1, MUX A1-A8
 *   U20 (0x24) = B-side J2, MUX B1-B8  (A2=1 pin tied HIGH on PCB)
 *   U19 on I2C1 bus  (SCL1=RF3 pin44, SDA1=RF2 pin1)
 *   U20 on I2C2 soft (SCL2=RD0,       SDA2=RF6      )
 *
 *   IO0[3:0] = SELECT (AS0-AS3 or BS0-BS3)
 *   IO0[7]=MUX1_EN  IO0[5]=MUX2_EN  IO0[6]=MUX5_EN  IO0[4]=MUX6_EN
 *   IO1[7]=MUX3_EN  IO1[5]=MUX4_EN  IO1[6]=MUX7_EN  IO1[4]=MUX8_EN
 *   All enable bits active LOW. IO1[3:0] unused, kept HIGH.
 *
 * A-side Z-lines (J1, PORTE/PORTF, 10k pull-up):
 *   A1->RF1(p4) A2->RE5(p8) A3->RE0(p15) A4->RE2(p11)
 *   A5->RF0(p5) A6->RE4(p9) A7->RE1(p14) A8->RE3(p10)
 * B-side Z-lines (J2, PORTB scrambled, 10k pull-up):
 *   B1->RB3(p22) B2->RB1(p20) B3->RB4(p23) B4->RB6(p25)
 *   B5->RB2(p21) B6->RB0(p19) B7->RB5(p24) B8->RB7(p26)
 *
 * LEDs: LED1=RB8(p27) LED2=RC13(p32) LED3=RC14(p35)
 *       LED4=RD3(p38) LED5=RD2(p41)
 * BTN:  BTN1=RE8(p36) BTN2=RD1(p37)  (10k ext pull-up)
 *
 * UART2: RF4=RX, RF5=TX, 115200 8N1 -> FT232 USB-COM
 * I2C1 : RF3=SCL(p44), RF2=SDA(p1),   ~205kHz  (U19)
 * I2C2 : RD0=SCL2,     RF6=SDA2,      ~200kHz  (U20, software bit-bang)
 *
 * Connector layout ? MUX channel assignment (from schematic U2/U3/U6/U11/U12/U15/U16/U16):
 *   mi=0 (U2)  ch0-15 : A1..A16
 *   mi=1 (U3)  ch0-15 : A17,B1..B15
 *   mi=2 (U6)  ch0-15 : B16,B17,C1..C14
 *   mi=3 (U11) ch0-15 : C15,C16,C17,D1..D13
 *   mi=4 (U12) ch0-15 : D14..D17,E1..E12
 *   mi=5 (U15) ch0-15 : E13..E16,F1..F12
 *   mi=6 (U16) ch0-15 : F13..F16,G1..G12
 *   mi=7 (U16) ch0-3  : G13..G16  (ch4-15 = NC)
 *
 * UART Protocol:
 *   MCU->PC: TESTER_READY | VERSION,x | PINS,116
 *            TEST_START,SIGNAL,<sn> | TEST_PASS1 | TEST_PASS2
 *            FAULT,<type>,<j1>,<j2> | TEST_DONE,<n> | RESULT,PASS/FAIL
 *   PC->MCU: T=start | X=bidir test | S,<sn>=set serial | V=version | P=ping
 *******************************************************************************/

#include <xc.h>
#include <stdint.h>

/* ?????????????????????????????????????????????????????????????????????????????
 * Configuration bits
 * ????????????????????????????????????????????????????????????????????????????? */
_FOSC(CSW_FSCM_OFF & XT_PLL8);   /* 7.3728 MHz XT + PLL8 -> Fosc=58.9824MHz, Fcy=Fosc/4=14.7456MHz */
_FWDT(WDT_OFF);
_FBORPOR(PBOR_OFF & MCLR_EN);
_FGS(CODE_PROT_OFF);

/* ?????????????????????????????????????????????????????????????????????????????
 * Timing constants
 * ????????????????????????????????????????????????????????????????????????????? */
/* dsPIC30F: FCY = FOSC/4 = (7.3728 MHz x PLL8)/4 = 58.9824/4 = 14.7456 MHz */
#define FCY         14745600UL

/* UART2: U2BRG = Fcy/(16*Baud) - 1 = 14745600/(16*115200) - 1 = 7 (exact, 0% error) */
#define U2BRG_VAL   7u

/* I2C1: I2CBRG = Fcy/(2*Fsck) - 1 = 14745600/(2*205000) - 1 = 35 -> Fsck~205kHz
 * Intentionally run at 205kHz (half spec) so PCA9555 reliably receives channel
 * select data even with PCB parasitic capacitance on SCL/SDA lines. */
#define I2CBRG_VAL  35u

/* ?????????????????????????????????????????????????????????????????????????????
 * Application constants
 * ????????????????????????????????????????????????????????????????????????????? */
#define FW_VERSION      "4.10"
#define TOTAL_PINS      116u
#define MUX_CH          16u

/* ?????????????????????????????????????????????????????????????????????????????
 * PCA9555 addresses and registers
 * ????????????????????????????????????????????????????????????????????????????? */
#define PCA_U19     0x20u   /* A-side */
#define PCA_U20     0x24u   /* B-side ? A2=1,A1=0,A0=0 confirmed by I2C scan */
#define PCA_OUT0    0x02u
#define PCA_OUT1    0x03u
#define PCA_CFG0    0x06u
#define PCA_CFG1    0x07u

/* PCA9555 enable bit masks (active LOW)
 * IO0: MUX1=bit7, MUX2=bit5, MUX5=bit6, MUX6=bit4
 * IO1: MUX3=bit7, MUX4=bit5, MUX7=bit6, MUX8=bit4 */
#define EN_IO0_MUX1   0x80u
#define EN_IO0_MUX2   0x20u
#define EN_IO0_MUX5   0x40u
#define EN_IO0_MUX6   0x10u
#define EN_IO1_MUX3   0x80u
#define EN_IO1_MUX4   0x20u
#define EN_IO1_MUX7   0x40u
#define EN_IO1_MUX8   0x10u

/* ?????????????????????????????????????????????????????????????????????????????
 * GPIO macros (XC16 bit-field style)
 * ????????????????????????????????????????????????????????????????????????????? */
#define LED_PASS    LATBbits.LATB8    /* pin 27 = RB8  */
#define LED_FAIL    LATCbits.LATC13   /* pin 32 = RC13 */
#define LED_BUSY    LATCbits.LATC14   /* pin 35 = RC14 */
#define LED_MODE    LATDbits.LATD3    /* pin 38 = RD3  */
#define LED_RDY     LATDbits.LATD2    /* pin 41 = RD2  (heartbeat) */
#define BTN1        PORTEbits.RE8     /* pin 36 = RE8  (10k ext pull-up) */
#define BTN2        PORTDbits.RD1     /* pin 37 = RD1  (10k ext pull-up) */

/* ?????????????????????????????????????????????????????????????????????????????
 * PIN TABLE ? const -> PSV (Flash), zero RAM cost
 * ????????????????????????????????????????????????????????????????????????????? */
typedef struct {
    uint8_t row;
    uint8_t col;
} PinDef;

/* PIN_TABLE[idx]: J1 connector pin at J1-side MUX scan index idx (0..115).
 * J1 MUX groups U2..U16 assign channels sequentially A1..G16 across connector rows.
 * idx = mi*16+ch  (mi=0..7 = MUX group, ch=0..15 = Y channel). */
static const PinDef PIN_TABLE[TOTAL_PINS] = {
    /*   0-  7 */ {'A', 1},{'A', 2},{'A', 3},{'A', 4},{'A', 5},{'A', 6},{'A', 7},{'A', 8},
    /*   8- 15 */ {'A', 9},{'A',10},{'A',11},{'A',12},{'A',13},{'A',14},{'A',15},{'A',16},
    /*  16- 23 */ {'A',17},{'B', 1},{'B', 2},{'B', 3},{'B', 4},{'B', 5},{'B', 6},{'B', 7},
    /*  24- 31 */ {'B', 8},{'B', 9},{'B',10},{'B',11},{'B',12},{'B',13},{'B',14},{'B',15},
    /*  32- 39 */ {'B',16},{'B',17},{'C', 1},{'C', 2},{'C', 3},{'C', 4},{'C', 5},{'C', 6},
    /*  40- 47 */ {'C', 7},{'C', 8},{'C', 9},{'C',10},{'C',11},{'C',12},{'C',13},{'C',14},
    /*  48- 55 */ {'C',15},{'C',16},{'C',17},{'D', 1},{'D', 2},{'D', 3},{'D', 4},{'D', 5},
    /*  56- 63 */ {'D', 6},{'D', 7},{'D', 8},{'D', 9},{'D',10},{'D',11},{'D',12},{'D',13},
    /*  64- 71 */ {'D',14},{'D',15},{'D',16},{'D',17},{'E', 1},{'E', 2},{'E', 3},{'E', 4},
    /*  72- 79 */ {'E', 5},{'E', 6},{'E', 7},{'E', 8},{'E', 9},{'E',10},{'E',11},{'E',12},
    /*  80- 87 */ {'E',13},{'E',14},{'E',15},{'E',16},{'F', 1},{'F', 2},{'F', 3},{'F', 4},
    /*  88- 95 */ {'F', 5},{'F', 6},{'F', 7},{'F', 8},{'F', 9},{'F',10},{'F',11},{'F',12},
    /*  96-103 */ {'F',13},{'F',14},{'F',15},{'F',16},{'G', 1},{'G', 2},{'G', 3},{'G', 4},
    /* 104-111 */ {'G', 5},{'G', 6},{'G', 7},{'G', 8},{'G', 9},{'G',10},{'G',11},{'G',12},
    /* 112-115 */ {'G',13},{'G',14},{'G',15},{'G',16}
};

/* J2 cable map indexed by J1 pin index:
 *   J1.A <-> J2.D,  J1.E <-> J2.G,  J1.B <-> J2.C,  J1.F <-> J2.F
 *   J1.C <-> J2.B,  J1.G <-> J2.E,  J1.D <-> J2.A  (same pin number within each pair) */
static const PinDef J2_PIN_TABLE[TOTAL_PINS] = {
    /* J1 Row A 0-16  -> J2 Row D */
    {'D', 1},{'D', 2},{'D', 3},{'D', 4},{'D', 5},{'D', 6},{'D', 7},{'D', 8},
    {'D', 9},{'D',10},{'D',11},{'D',12},{'D',13},{'D',14},{'D',15},{'D',16},{'D',17},
    /* J1 Row E 17-32 -> J2 Row G */
    {'G', 1},{'G', 2},{'G', 3},{'G', 4},{'G', 5},{'G', 6},{'G', 7},{'G', 8},
    {'G', 9},{'G',10},{'G',11},{'G',12},{'G',13},{'G',14},{'G',15},{'G',16},
    /* J1 Row B 33-49 -> J2 Row C */
    {'C', 1},{'C', 2},{'C', 3},{'C', 4},{'C', 5},{'C', 6},{'C', 7},{'C', 8},
    {'C', 9},{'C',10},{'C',11},{'C',12},{'C',13},{'C',14},{'C',15},{'C',16},{'C',17},
    /* J1 Row F 50-65 -> J2 Row F */
    {'F', 1},{'F', 2},{'F', 3},{'F', 4},{'F', 5},{'F', 6},{'F', 7},{'F', 8},
    {'F', 9},{'F',10},{'F',11},{'F',12},{'F',13},{'F',14},{'F',15},{'F',16},
    /* J1 Row C 66-82 -> J2 Row B */
    {'B', 1},{'B', 2},{'B', 3},{'B', 4},{'B', 5},{'B', 6},{'B', 7},{'B', 8},
    {'B', 9},{'B',10},{'B',11},{'B',12},{'B',13},{'B',14},{'B',15},{'B',16},{'B',17},
    /* J1 Row G 83-98 -> J2 Row E */
    {'E', 1},{'E', 2},{'E', 3},{'E', 4},{'E', 5},{'E', 6},{'E', 7},{'E', 8},
    {'E', 9},{'E',10},{'E',11},{'E',12},{'E',13},{'E',14},{'E',15},{'E',16},
    /* J1 Row D 99-115 -> J2 Row A */
    {'A', 1},{'A', 2},{'A', 3},{'A', 4},{'A', 5},{'A', 6},{'A', 7},{'A', 8},
    {'A', 9},{'A',10},{'A',11},{'A',12},{'A',13},{'A',14},{'A',15},{'A',16},{'A',17}
};


/* ?????????????????????????????????????????????????????????????????????????????
 * RAM variables
 * ????????????????????????????????????????????????????????????????????????????? */
static char g_serial[24] = "SN-000000";

/* PCA9555 shadow registers */
static uint8_t u19_io0 = 0xFF;
static uint8_t u19_io1 = 0xFF;
static uint8_t u20_io0 = 0xFF;
static uint8_t u20_io1 = 0xFF;

/* UART receive buffer */
static char    rx_buf[48];
static uint8_t rx_pos = 0;

static volatile uint8_t g_testing    = 0u;
static volatile uint8_t g_i2c_error  = 0u;   /* I2C1 (U19) error flag */
static volatile uint8_t g_i2c2_error = 0u;   /* I2C2 soft (U20) error flag */

/* ?????????????????????????????????????????????????????????????????????????????
 * Delay
 * ????????????????????????????????????????????????????????????????????????????? */
static void delay_ms(uint16_t ms) {
    uint16_t i;
    uint16_t j;
    for (i = 0; i < ms; i++)
        for (j = 0; j < (uint16_t)(FCY / 10000UL); j++);
}

static void delay_us(uint16_t us) {
    uint16_t i;
    /* At Fcy=29.49MHz, 1us ~ 29 cycles. Each loop iteration ~ 2 cycles */
    for (i = 0; i < us; i++)
        __asm__ volatile("repeat #14; nop");
}

/* ?????????????????????????????????????????????????????????????????????????????
 * Timer1 ? 1 Hz heartbeat blink on LED_RDY (toggle every 500 ms)
 * Fcy=14745600, prescaler 1:256 -> tick=57600 Hz, PR1=28799 -> 500 ms
 * ????????????????????????????????????????????????????????????????????????????? */
static void timer1_init(void) {
    T1CON = 0x0030u;        /* TCKPS=11: 1:256 prescaler, timer off */
    PR1   = 28799u;
    TMR1  = 0u;
    IPC0bits.T1IP = 4u;
    IFS0bits.T1IF = 0u;
    IEC0bits.T1IE = 1u;
    T1CONbits.TON = 1u;
}

void __attribute__((interrupt, no_auto_psv)) _T1Interrupt(void) {
    IFS0bits.T1IF = 0u;
    if (!g_testing)
        LATD ^= (uint16_t)(1u << 2u);  /* toggle LED_RDY (LATD2 = RD2) */
}

/* ?????????????????????????????????????????????????????????????????????????????
 * UART2
 * RF4=U2RX, RF5=U2TX
 * ????????????????????????????????????????????????????????????????????????????? */
static void uart_init(void) {
    U2BRG  = U2BRG_VAL;
    U2MODE = 0x8000u;   /* UARTEN=1, 8N1 */
    U2STA  = 0x0400u;   /* UTXEN=1 */
}

static void uart_putc(char c) {
    while (U2STAbits.UTXBF);
    U2TXREG = (uint8_t)c;
}

static void uart_puts(const char *s) {
    while (*s) uart_putc(*s++);
}

static void uart_putuint(uint16_t val) {
    uint8_t digits[5];
    uint8_t i;
    uint8_t len = 0;
    if (val == 0u) { uart_putc('0'); return; }
    while (val > 0u) {
        digits[len++] = (uint8_t)(val % 10u);
        val /= 10u;
    }
    for (i = len; i > 0u; i--)
        uart_putc((char)('0' + digits[i - 1u]));
}

/* Print uint8 as two uppercase hex digits e.g. 0x24 -> "24" */
static void uart_puthex8(uint8_t val) {
    const char *hex = "0123456789ABCDEF";
    uart_putc(hex[(val >> 4u) & 0x0Fu]);
    uart_putc(hex[val & 0x0Fu]);
}

/* "KEY,number\r\n" helper */
static void uart_kn(const char *key, uint16_t n) {
    uart_puts(key);
    uart_putc(',');
    uart_putuint(n);
    uart_puts("\r\n");
}

/* ?????????????????????????????????????????????????????????????????????????????
 * Pin name: linear index -> "A1".."D17"
 * ????????????????????????????????????????????????????????????????????????????? */
static void pin_name(uint8_t p, char *buf) {
    uint8_t col;
    uint8_t len;
    uint8_t i;
    uint8_t digits[3];

    if (p >= TOTAL_PINS) {
        buf[0] = '?'; buf[1] = '\0';
        return;
    }
    buf[0] = (char)PIN_TABLE[p].row;
    col    = PIN_TABLE[p].col;
    len    = 0;
    do {
        digits[len++] = col % 10u;
        col /= 10u;
    } while (col > 0u);
    for (i = len; i > 0u; i--)
        buf[1u + (len - i)] = (char)('0' + digits[i - 1u]);
    buf[1u + len] = '\0';
}

/* J2_PHYSICAL_MAP[rp]: the actual J2 cable pin at MUX scan position rp.
 *
 * scan_pin() calls pin2mux(rp) for rp=0..115 to select J2 channels.
 * Because the J2 connector is wired into the MUX in a different physical
 * order than J1, J2_PHYSICAL_MAP translates MUX scan index -> real pin name.
 *
 * This table is derived from the J-command Pass-2 scan order (which drives
 * each J2 pin individually and reports the physical MUX scan sequence) and
 * independently verified by F-row debug output cross-referenced against
 * manual continuity measurement.  58/58 confirmed positions match.
 *
 *   rp   0- 15: D-even/F-odd interleaved (D2,F3,D4,F5,...D16), then B1
 *   rp  16- 34: A1, C-even/C-odd interleaved, C15, C17
 *   rp  34- 50: F1, G2/G1, G4/G3, ..., G16/G15
 *   rp  51- 67: D1, B2/D3, B4/D5, ..., B16/D17
 *   rp  68- 83: A2..A17, E2
 *   rp  84- 98: E3..E16
 *   rp  99-106: B3,B5,B7,B9,B11,B13,B15,B17
 *   rp 107-114: F2,F4,F6,F8,F10,F12,F14,F16
 *   rp 115:     E1
 */
/* J2_PHYSICAL_MAP[rp]: J2 connector pin at B-side MUX scan position rp (0..115).
 * J2 MUX groups U4..U18 use identical channel assignment as J1 (same schematic layout).
 * Therefore J2_PHYSICAL_MAP[rp] == PIN_TABLE[rp] ? both index sequentially A1..G16.
 * Verified: 66/66 signal pairs match manual continuity measurement. */
static const PinDef J2_PHYSICAL_MAP[TOTAL_PINS] = {
    /*   0-  7 */ {'A', 1},{'A', 2},{'A', 3},{'A', 4},{'A', 5},{'A', 6},{'A', 7},{'A', 8},
    /*   8- 15 */ {'A', 9},{'A',10},{'A',11},{'A',12},{'A',13},{'A',14},{'A',15},{'A',16},
    /*  16- 23 */ {'A',17},{'B', 1},{'B', 2},{'B', 3},{'B', 4},{'B', 5},{'B', 6},{'B', 7},
    /*  24- 31 */ {'B', 8},{'B', 9},{'B',10},{'B',11},{'B',12},{'B',13},{'B',14},{'B',15},
    /*  32- 39 */ {'B',16},{'B',17},{'C', 1},{'C', 2},{'C', 3},{'C', 4},{'C', 5},{'C', 6},
    /*  40- 47 */ {'C', 7},{'C', 8},{'C', 9},{'C',10},{'C',11},{'C',12},{'C',13},{'C',14},
    /*  48- 55 */ {'C',15},{'C',16},{'C',17},{'D', 1},{'D', 2},{'D', 3},{'D', 4},{'D', 5},
    /*  56- 63 */ {'D', 6},{'D', 7},{'D', 8},{'D', 9},{'D',10},{'D',11},{'D',12},{'D',13},
    /*  64- 71 */ {'D',14},{'D',15},{'D',16},{'D',17},{'E', 1},{'E', 2},{'E', 3},{'E', 4},
    /*  72- 79 */ {'E', 5},{'E', 6},{'E', 7},{'E', 8},{'E', 9},{'E',10},{'E',11},{'E',12},
    /*  80- 87 */ {'E',13},{'E',14},{'E',15},{'E',16},{'F', 1},{'F', 2},{'F', 3},{'F', 4},
    /*  88- 95 */ {'F', 5},{'F', 6},{'F', 7},{'F', 8},{'F', 9},{'F',10},{'F',11},{'F',12},
    /*  96-103 */ {'F',13},{'F',14},{'F',15},{'F',16},{'G', 1},{'G', 2},{'G', 3},{'G', 4},
    /* 104-111 */ {'G', 5},{'G', 6},{'G', 7},{'G', 8},{'G', 9},{'G',10},{'G',11},{'G',12},
    /* 112-115 */ {'G',13},{'G',14},{'G',15},{'G',16}
};

/* Return the J2 cable pin name for a given J2 MUX scan position rp (0..115).
 * Uses J2_PHYSICAL_MAP ? hardware-verified translation from scan index
 * to the actual connector pin reached at that MUX group/channel. */
static void j2pin_name(uint8_t rp, char *buf) {
    uint8_t col;
    uint8_t len;
    uint8_t i;
    uint8_t digits[3];

    if (rp >= TOTAL_PINS) { buf[0] = '?'; buf[1] = '\0'; return; }

    buf[0] = (char)J2_PHYSICAL_MAP[rp].row;
    col    = J2_PHYSICAL_MAP[rp].col;
    len    = 0u;
    do {
        digits[len++] = col % 10u;
        col /= 10u;
    } while (col > 0u);
    for (i = len; i > 0u; i--)
        buf[1u + (len - i)] = (char)('0' + digits[i - 1u]);
    buf[1u + len] = '\0';
}

/* ?????????????????????????????????????????????????????????????????????????????
 * I2C1 ? manual SFR driver (RF3=SCL1, RF2=SDA1) ? used for U19
 * Each wait loop has a ~4ms hardware timeout.  On bus lockup the flag
 * g_i2c_error is set and all subsequent I2C calls return immediately,
 * so run_test() can detect the fault and report it to the PC instead of
 * hanging until the PC-side 120s timer fires.
 * ????????????????????????????????????????????????????????????????????????????? */
#define I2C_TIMEOUT 30000u   /* ~4ms at Fcy=29.49MHz, 4 cycles/iter */

static void i2c_init(void) {
    I2CBRG = I2CBRG_VAL;
    I2CCONbits.I2CEN = 1;
}

static void i2c_start(void) {
    uint16_t t = 0u;
    if (g_i2c_error) return;
    I2CCONbits.SEN = 1;
    while (I2CCONbits.SEN) {
        if (++t > I2C_TIMEOUT) { g_i2c_error = 1u; return; }
    }
}

static void i2c_stop(void) {
    uint16_t t = 0u;
    if (g_i2c_error) return;
    I2CCONbits.PEN = 1;
    while (I2CCONbits.PEN) {
        if (++t > I2C_TIMEOUT) { g_i2c_error = 1u; return; }
    }
}

static void i2c_write(uint8_t data) {
    uint16_t t = 0u;
    if (g_i2c_error) return;
    I2CTRN = data;
    while (I2CSTATbits.TBF) {
        if (++t > I2C_TIMEOUT) { g_i2c_error = 1u; return; }
    }
    t = 0u;
    while (I2CSTATbits.TRSTAT) {
        if (++t > I2C_TIMEOUT) { g_i2c_error = 1u; return; }
    }
}

/* Probe one I2C1 address ? returns 1 if device ACKs, 0 if NACK/absent */
static uint8_t i2c_probe(uint8_t addr) {
    uint8_t acked;
    g_i2c_error = 0u;
    i2c_start();
    if (g_i2c_error) { g_i2c_error = 0u; return 0u; }
    I2CTRN = (uint8_t)((addr << 1u) | 0x00u);
    {
        uint16_t t = 0u;
        while (I2CSTATbits.TBF)   { if (++t > I2C_TIMEOUT) { g_i2c_error = 1u; break; } }
        t = 0u;
        while (I2CSTATbits.TRSTAT){ if (++t > I2C_TIMEOUT) { g_i2c_error = 1u; break; } }
    }
    acked = (uint8_t)((g_i2c_error == 0u && I2CSTATbits.ACKSTAT == 0u) ? 1u : 0u);
    g_i2c_error = 0u;
    i2c_stop();
    g_i2c_error = 0u;
    delay_us(50u);
    return acked;
}

/* ?????????????????????????????????????????????????????????????????????????????
 * I2C2 ? software bit-bang (RD0=SCL2, RF6=SDA2) ? used for U20
 *
 * Open-drain convention:
 *   SCL: driven output (master only, no clock stretch needed)
 *        HIGH -> LATD0=1, TRISD0=0   LOW -> LATD0=0, TRISD0=0
 *   SDA: open-drain via TRIS
 *        release (HIGH) -> TRISF6=1  drive LOW -> LATF6=0, TRISF6=0
 *
 * Timing: delay_us(5) half-period -> ~100kHz effective (conservative).
 * ????????????????????????????????????????????????????????????????????????????? */
#define I2C2_SCL_HIGH()  do { LATDbits.LATD0   = 1u; TRISDbits.TRISD0 = 0u; } while(0)
#define I2C2_SCL_LOW()   do { LATDbits.LATD0   = 0u; TRISDbits.TRISD0 = 0u; } while(0)
#define I2C2_SDA_REL()   do { TRISFbits.TRISF6 = 1u; } while(0)
#define I2C2_SDA_LOW()   do { LATFbits.LATF6   = 0u; TRISFbits.TRISF6 = 0u; } while(0)
#define I2C2_SDA_RD()    (PORTFbits.RF6)

static void i2c2_init(void) {
    LATFbits.LATF6   = 0u;       /* SDA2 LAT preset 0 (driven LOW when TRIS=0) */
    TRISFbits.TRISF6 = 1u;       /* SDA2 = input (released HIGH via PCB pull-up) */
    LATDbits.LATD0   = 1u;
    TRISDbits.TRISD0 = 0u;       /* SCL2 = output HIGH (idle) */
    delay_us(10u);
}

static void i2c2_start(void) {
    if (g_i2c2_error) return;
    I2C2_SDA_REL();
    I2C2_SCL_HIGH();
    delay_us(5u);
    I2C2_SDA_LOW();    /* SDA falls while SCL high = START */
    delay_us(5u);
    I2C2_SCL_LOW();
    delay_us(5u);
}

static void i2c2_stop(void) {
    if (g_i2c2_error) return;
    I2C2_SDA_LOW();
    delay_us(2u);
    I2C2_SCL_HIGH();
    delay_us(5u);
    I2C2_SDA_REL();    /* SDA rises while SCL high = STOP */
    delay_us(5u);
}

static void i2c2_rep_start(void) {
    if (g_i2c2_error) return;
    I2C2_SDA_REL();
    delay_us(2u);
    I2C2_SCL_HIGH();
    delay_us(5u);
    I2C2_SDA_LOW();
    delay_us(5u);
    I2C2_SCL_LOW();
    delay_us(2u);
}

/* Send one byte; sets g_i2c2_error on NACK */
static void i2c2_write(uint8_t data) {
    uint8_t i;
    if (g_i2c2_error) return;
    for (i = 0u; i < 8u; i++) {
        if (data & 0x80u) I2C2_SDA_REL();
        else              I2C2_SDA_LOW();
        delay_us(2u);
        I2C2_SCL_HIGH();
        delay_us(5u);
        I2C2_SCL_LOW();
        delay_us(2u);
        data = (uint8_t)(data << 1u);
    }
    /* ACK bit: release SDA, clock, read */
    I2C2_SDA_REL();
    delay_us(2u);
    I2C2_SCL_HIGH();
    delay_us(3u);
    if (I2C2_SDA_RD()) g_i2c2_error = 1u;   /* NACK */
    I2C2_SCL_LOW();
    delay_us(2u);
}

/* Read one byte, always sends NACK (single-byte read) */
static uint8_t i2c2_read_byte(void) {
    uint8_t i;
    uint8_t val = 0u;
    if (g_i2c2_error) return 0xFFu;
    I2C2_SDA_REL();
    for (i = 0u; i < 8u; i++) {
        I2C2_SCL_HIGH();
        delay_us(5u);
        val = (uint8_t)((val << 1u) | (I2C2_SDA_RD() ? 1u : 0u));
        I2C2_SCL_LOW();
        delay_us(2u);
    }
    /* Send NACK: SDA released (high) during ACK clock */
    I2C2_SDA_REL();
    delay_us(2u);
    I2C2_SCL_HIGH();
    delay_us(5u);
    I2C2_SCL_LOW();
    delay_us(2u);
    return val;
}

/* Probe one I2C2 address ? returns 1 if device ACKs, 0 if NACK/absent */
static uint8_t i2c2_probe(uint8_t addr) {
    uint8_t acked;
    g_i2c2_error = 0u;
    i2c2_start();
    if (g_i2c2_error) { g_i2c2_error = 0u; return 0u; }
    i2c2_write((uint8_t)((addr << 1u) | 0x00u));
    acked = (uint8_t)(g_i2c2_error ? 0u : 1u);
    g_i2c2_error = 0u;
    i2c2_stop();
    g_i2c2_error = 0u;
    delay_us(50u);
    return acked;
}

/* ?????????????????????????????????????????????????????????????????????????????
 * PCA9555 ? I2C1 (U19)
 * ????????????????????????????????????????????????????????????????????????????? */

/* Repeated-START for master-read (I2C1) */
static void i2c_rep_start(void) {
    uint16_t t = 0u;
    if (g_i2c_error) return;
    I2CCONbits.RSEN = 1;
    while (I2CCONbits.RSEN) {
        if (++t > I2C_TIMEOUT) { g_i2c_error = 1u; return; }
    }
}

/* Master read ? always NACKs (single-byte read) */
static uint8_t i2c_read_byte(void) {
    uint16_t t = 0u;
    if (g_i2c_error) return 0xFFu;
    I2CCONbits.RCEN = 1;
    while (!I2CSTATbits.RBF) {
        if (++t > I2C_TIMEOUT) { g_i2c_error = 1u; return 0xFFu; }
    }
    I2CCONbits.ACKDT = 1;   /* NACK = tell slave we are done */
    I2CCONbits.ACKEN = 1;
    t = 0u;
    while (I2CCONbits.ACKEN) {
        if (++t > I2C_TIMEOUT) { g_i2c_error = 1u; return 0xFFu; }
    }
    return I2CRCV;
}

/* Read one byte from a PCA9555 register via I2C1 */
static uint8_t pca_read_reg(uint8_t addr, uint8_t reg) {
    uint8_t val;
    i2c_start();
    i2c_write((uint8_t)((addr << 1u) | 0x00u));
    i2c_write(reg);
    i2c_rep_start();
    i2c_write((uint8_t)((addr << 1u) | 0x01u));
    val = i2c_read_byte();
    i2c_stop();
    return val;
}

static void pca_write(uint8_t addr, uint8_t reg, uint8_t val) {
    i2c_start();
    i2c_write((uint8_t)((addr << 1u) | 0x00u));
    i2c_write(reg);
    i2c_write(val);
    i2c_stop();
}

/* ?????????????????????????????????????????????????????????????????????????????
 * PCA9555 ? I2C2 soft (U20)
 * Errors propagate to g_i2c_error so all existing loop guards still work.
 * ????????????????????????????????????????????????????????????????????????????? */
static void pca2_write(uint8_t addr, uint8_t reg, uint8_t val) {
    if (g_i2c_error) return;
    i2c2_start();
    i2c2_write((uint8_t)((addr << 1u) | 0x00u));
    i2c2_write(reg);
    i2c2_write(val);
    i2c2_stop();
    if (g_i2c2_error) g_i2c_error = 1u;
}

static uint8_t pca2_read_reg(uint8_t addr, uint8_t reg) {
    uint8_t val;
    if (g_i2c_error) return 0xFFu;
    i2c2_start();
    i2c2_write((uint8_t)((addr << 1u) | 0x00u));
    i2c2_write(reg);
    i2c2_rep_start();
    i2c2_write((uint8_t)((addr << 1u) | 0x01u));
    val = i2c2_read_byte();
    i2c2_stop();
    if (g_i2c2_error) g_i2c_error = 1u;
    return val;
}

static void pca_init(void) {
    /* All pins = outputs */
    pca_write(PCA_U19,  PCA_CFG0, 0x00u);   /* I2C1 */
    pca_write(PCA_U19,  PCA_CFG1, 0x00u);
    pca2_write(PCA_U20, PCA_CFG0, 0x00u);   /* I2C2 */
    pca2_write(PCA_U20, PCA_CFG1, 0x00u);
    /* All enables HIGH (disabled), select = 0xF (channel 15) */
    u19_io0 = 0xFF; u19_io1 = 0xFF;
    u20_io0 = 0xFF; u20_io1 = 0xFF;
    pca_write(PCA_U19,  PCA_OUT0, u19_io0);
    pca_write(PCA_U19,  PCA_OUT1, u19_io1);
    pca2_write(PCA_U20, PCA_OUT0, u20_io0);
    pca2_write(PCA_U20, PCA_OUT1, u20_io1);
}

/* ?????????????????????????????????????????????????????????????????????????????
 * MUX control
 * ????????????????????????????????????????????????????????????????????????????? */

/* Enable bit lookup per mux index (0=MUX1 .. 7=MUX8) */
static const uint8_t MUX_IO0_MASK[8] = {
    EN_IO0_MUX1, EN_IO0_MUX2, 0x00u,       0x00u,
    EN_IO0_MUX5, EN_IO0_MUX6, 0x00u,       0x00u
};
static const uint8_t MUX_IO1_MASK[8] = {
    0x00u,       0x00u,       EN_IO1_MUX3, EN_IO1_MUX4,
    0x00u,       0x00u,       EN_IO1_MUX7, EN_IO1_MUX8
};

static void mux_off(void) {
    u19_io0 = 0xFF; u19_io1 = 0xFF;
    u20_io0 = 0xFF; u20_io1 = 0xFF;
    pca_write(PCA_U19,  PCA_OUT0, u19_io0);   /* I2C1 */
    pca_write(PCA_U19,  PCA_OUT1, u19_io1);
    pca2_write(PCA_U20, PCA_OUT0, u20_io0);   /* I2C2 */
    pca2_write(PCA_U20, PCA_OUT1, u20_io1);
}

/* Select channel ch on A-side mux mi (0-7) ? U19 via I2C1 */
static void mux_a(uint8_t mi, uint8_t ch) {
    u19_io0 = 0xFF;
    u19_io1 = 0xFF;
    /* Set channel in IO0[3:0] */
    u19_io0 = (uint8_t)((u19_io0 & 0xF0u) | (ch & 0x0Fu));
    /* Clear enable bit (active LOW) */
    if (MUX_IO0_MASK[mi] != 0x00u)
        u19_io0 &= (uint8_t)(~MUX_IO0_MASK[mi]);
    else
        u19_io1 &= (uint8_t)(~MUX_IO1_MASK[mi]);
    pca_write(PCA_U19, PCA_OUT0, u19_io0);
    pca_write(PCA_U19, PCA_OUT1, u19_io1);
}

/* Select channel ch on B-side mux mi (0-7) ? U20 via I2C2 */
static void mux_b(uint8_t mi, uint8_t ch) {
    u20_io0 = 0xFF;
    u20_io1 = 0xFF;
    u20_io0 = (uint8_t)((u20_io0 & 0xF0u) | (ch & 0x0Fu));
    if (MUX_IO0_MASK[mi] != 0x00u)
        u20_io0 &= (uint8_t)(~MUX_IO0_MASK[mi]);
    else
        u20_io1 &= (uint8_t)(~MUX_IO1_MASK[mi]);
    pca2_write(PCA_U20, PCA_OUT0, u20_io0);   /* I2C2 */
    pca2_write(PCA_U20, PCA_OUT1, u20_io1);
}

static void pin2mux(uint8_t p, uint8_t *mi, uint8_t *ch) {
    *mi = p / MUX_CH;
    *ch = p % MUX_CH;
}

/* ?????????????????????????????????????????????????????????????????????????????
 * Z-line pin tables (from schematic)
 *
 * A-side Z (J1): PORTF (mi=0,4) or PORTE (mi=1,2,3,5,6,7)
 *   mi: 0=A1 1=A2 2=A3 3=A4 4=A5 5=A6 6=A7 7=A8
 * B-side Z (J2): PORTB, scrambled bit order
 *   mi: 0=B1 1=B2 2=B3 3=B4 4=B5 5=B6 6=B7 7=B8
 * ????????????????????????????????????????????????????????????????????????????? */
static const uint8_t ZA_IS_RF[8] = {1u, 0u, 0u, 0u, 1u, 0u, 0u, 0u};
static const uint8_t ZA_BIT[8]   = {1u, 5u, 0u, 2u, 0u, 4u, 1u, 3u};
static const uint8_t ZB_BIT[8]   = {3u, 1u, 4u, 6u, 2u, 0u, 5u, 7u};

/* A-side: drive J1 pin LOW through mux (PORTE or PORTF, digital, no ADPCFG) */
static void za_drive_low(uint8_t mi) {
    uint16_t mask = (uint16_t)(1u << ZA_BIT[mi]);
    if (ZA_IS_RF[mi]) {
        TRISF &= (uint16_t)(~mask);
        LATF  &= (uint16_t)(~mask);
    } else {
        TRISE &= (uint16_t)(~mask);
        LATE  &= (uint16_t)(~mask);
    }
}

static void za_release(uint8_t mi) {
    uint16_t mask = (uint16_t)(1u << ZA_BIT[mi]);
    if (ZA_IS_RF[mi]) TRISF |= mask;
    else               TRISE |= mask;
}

/* Returns 1 if HIGH (open), 0 if LOW (connected) ? reads A-side (J1)
 * Double-read: if two consecutive reads agree the result is stable.
 * 50us settle covers RC = 10k x 5nF (long cable) to reach 99.3% of final value. */
static uint8_t za_read(uint8_t mi) {
    uint8_t bit  = ZA_BIT[mi];
    uint16_t mask = (uint16_t)(1u << bit);
    uint8_t v1, v2;

    if (ZA_IS_RF[mi]) {
        TRISF |= mask;
        delay_us(50u);
        v1 = (uint8_t)((PORTF >> bit) & 0x01u);
        delay_us(10u);
        v2 = (uint8_t)((PORTF >> bit) & 0x01u);
        if (v1 == v2) return v1;
        delay_us(50u);
        return (uint8_t)((PORTF >> bit) & 0x01u);   /* tiebreaker */
    } else {
        TRISE |= mask;
        delay_us(50u);
        v1 = (uint8_t)((PORTE >> bit) & 0x01u);
        delay_us(10u);
        v2 = (uint8_t)((PORTE >> bit) & 0x01u);
        if (v1 == v2) return v1;
        delay_us(50u);
        return (uint8_t)((PORTE >> bit) & 0x01u);   /* tiebreaker */
    }
}

/* B-side: drive J2 pin LOW through mux (PORTB scrambled, AN pins need ADPCFG) */
static void zb_drive_low(uint8_t mi) {
    uint16_t mask = (uint16_t)(1u << ZB_BIT[mi]);
    ADPCFG |= mask;                  /* digital mode */
    TRISB  &= (uint16_t)(~mask);    /* output */
    LATB   &= (uint16_t)(~mask);    /* LOW */
}

static void zb_release(uint8_t mi) {
    TRISB |= (uint16_t)(1u << ZB_BIT[mi]);   /* input ? PCB pull-up restores HIGH */
}

/* Returns 1 if HIGH (open), 0 if LOW (connected) ? reads B-side (J2) */
static uint8_t zb_read(uint8_t mi) {
    uint8_t  bit  = ZB_BIT[mi];
    uint16_t mask = (uint16_t)(1u << bit);
    uint8_t  v1, v2;
    ADPCFG |= mask;
    TRISB  |= mask;
    delay_us(50u);   /* settle: 10k pull-up x cable RC */
    v1 = (uint8_t)((PORTB >> bit) & 0x01u);
    delay_us(10u);
    v2 = (uint8_t)((PORTB >> bit) & 0x01u);
    if (v1 == v2) return v1;
    delay_us(50u);
    return (uint8_t)((PORTB >> bit) & 0x01u);    /* tiebreaker */
}

/* ?????????????????????????????????????????????????????????????????????????????
 * Scan one J1 pin ? drive it LOW, read all J2 pins.
 * Sends "CONN,<j1>,<j2>\r\n" for every J2 pin found LOW (connected).
 * Returns number of connections found.
 * ????????????????????????????????????????????????????????????????????????????? */
static uint16_t scan_pin(uint8_t drv) {
    uint8_t  drv_mi, drv_ch;
    uint8_t  rp, rm, rc;
    uint16_t found = 0u;
    char     sa[6], sb[6];

    pin2mux(drv, &drv_mi, &drv_ch);
    mux_a(drv_mi, drv_ch);
    za_drive_low(drv_mi);
    delay_us(100u);
    pin_name(drv, sa);

    for (rp = 0u; rp < TOTAL_PINS; rp++) {
        if (g_i2c_error) break;
        pin2mux(rp, &rm, &rc);
        delay_us(10);
        mux_b(rm, rc);
        if (zb_read(rm) == 0u) {   /* LOW = connected */
            j2pin_name(rp, sb);
            uart_puts("CONN,");
            uart_puts(sa);
            uart_putc(',');
            uart_puts(sb);
            uart_puts("\r\n");
            found++;
        }
    }

    za_release(drv_mi);
    mux_off();
    return found;
}

/* ?????????????????????????????????????????????????????????????????????????????
 * BIDIRECTIONAL SCAN FUNCTIONS  (v4.4)
 *
 * Approach: drive one pin LOW, scan ALL pins on the other connector.
 *
 *   scan_j1_drive(j1_idx)  ? set J1[j1_idx]=0, read every J2 pin
 *   scan_j2_drive(j2_idx)  ? set J2[j2_idx]=0, read every J1 pin
 *
 * Both use PIN_TABLE directly for naming ? no arbitrary offset.
 * Output format:  CONN,J1.<name>,J2.<name>\r\n
 *
 * Cross-check rule: if J1->J2 and J2->J1 report the same pairs the
 * mapping is correct.  If they disagree the PCB wiring offset is wrong.
 * ????????????????????????????????????????????????????????????????????????????? */

/* Pass A: drive J1 pin j1_idx LOW via MUX-A + ZA, read every J2 pin. */
static uint16_t scan_j1_drive(uint8_t j1_idx) {
    uint8_t  mi_a, ch_a;
    uint8_t  j2_idx, mi_b, ch_b;
    uint16_t found = 0u;
    char     sa[6], sb[6];

    pin2mux(j1_idx, &mi_a, &ch_a);

    /* Select J1 cable pin through MUX-A, drive Z-line LOW */
    mux_a(mi_a, ch_a);
    za_drive_low(mi_a);
    delay_us(200u);          /* longer settle: 10k pull-up + cable RC */

    pin_name(j1_idx, sa);

    for (j2_idx = 0u; j2_idx < TOTAL_PINS; j2_idx++) {
        if (g_i2c_error) break;
        pin2mux(j2_idx, &mi_b, &ch_b);
        mux_b(mi_b, ch_b);   /* mux_b writes PCA_U20 only ? A-side stays */
        delay_us(100u);       /* settle after MUX-B channel switch */
        if (zb_read(mi_b) == 0u) {   /* LOW = continuity */
            j2pin_name(j2_idx, sb);
            uart_puts("CONN,J1.");
            uart_puts(sa);
            uart_puts(",J2.");
            uart_puts(sb);
            uart_puts("\r\n");
            found++;
        }
    }

    za_release(mi_a);
    mux_off();
    return found;
}

/* Pass B: drive J2 pin j2_idx LOW via MUX-B + ZB, read every J1 pin. */
static uint16_t scan_j2_drive(uint8_t j2_idx) {
    uint8_t  mi_b, ch_b;
    uint8_t  j1_idx, mi_a, ch_a;
    uint16_t found = 0u;
    char     sa[6], sb[6];

    pin2mux(j2_idx, &mi_b, &ch_b);

    /* Select J2 cable pin through MUX-B, drive Z-line LOW */
    mux_b(mi_b, ch_b);
    zb_drive_low(mi_b);
    delay_us(200u);          /* settle */

    for (j1_idx = 0u; j1_idx < TOTAL_PINS; j1_idx++) {
        if (g_i2c_error) break;
        pin2mux(j1_idx, &mi_a, &ch_a);
        mux_a(mi_a, ch_a);   /* mux_a writes PCA_U19 only ? B-side stays */
        delay_us(100u);       /* settle after MUX-A channel switch */
        /* za_read sets pin as input, waits 50us, then reads */
        if (za_read(mi_a) == 0u) {   /* LOW = continuity */
            pin_name(j1_idx, sa);
            j2pin_name(j2_idx, sb);
            uart_puts("CONN,J1.");
            uart_puts(sa);
            uart_puts(",J2.");
            uart_puts(sb);
            uart_puts("\r\n");
            found++;
        }
    }

    zb_release(mi_b);
    mux_off();
    return found;
}

/* Bidirectional test:
 *   PASS_A  ? scan_j1_drive for every J1 pin (J1 drives, J2 reads)
 *   PASS_B  ? scan_j2_drive for every J2 pin (J2 drives, J1 reads)
 * If PASS_A and PASS_B report identical pairs -> mapping is correct.
 */
static void run_test_bidir(void) {
    uint8_t  idx;
    uint16_t total_a = 0u;
    uint16_t total_b = 0u;

    g_i2c_error = 0u; g_i2c2_error = 0u;

    uart_puts("TEST_START,");
    uart_puts(g_serial);
    uart_puts("\r\n");

    /* ??? Pass A: J1 pin drives, scan J2 ??????????????????????????????? */
    uart_puts("PASS_A_START\r\n");
    for (idx = 0u; idx < TOTAL_PINS; idx++) {
        if (g_i2c_error) break;
        total_a += scan_j1_drive(idx);
    }
    if (g_i2c_error) {
        uart_puts("ERROR,I2C_TIMEOUT_PASS_A\r\n");
        LED_FAIL = 1;
        g_i2c_error = 0u; g_i2c2_error = 0u;
        return;
    }
    uart_puts("PASS_A_DONE,");
    uart_putuint(total_a);
    uart_puts("\r\n");

    /* ??? Pass B: J2 pin drives, scan J1 ??????????????????????????????? */
    uart_puts("PASS_B_START\r\n");
    for (idx = 0u; idx < TOTAL_PINS; idx++) {
        if (g_i2c_error) break;
        total_b += scan_j2_drive(idx);
    }
    if (g_i2c_error) {
        uart_puts("ERROR,I2C_TIMEOUT_PASS_B\r\n");
        LED_FAIL = 1;
        g_i2c_error = 0u; g_i2c2_error = 0u;
        return;
    }
    uart_puts("PASS_B_DONE,");
    uart_putuint(total_b);
    uart_puts("\r\n");

    /* total_a == total_b when both passes find the same connections */
    uart_kn("TEST_DONE", total_a);
    if (total_a == total_b) {
        LED_PASS = 1; LED_FAIL = 0;
    } else {
        /* Pass counts differ: likely a pin-mapping offset problem */
        uart_puts("WARN,PASS_A_B_MISMATCH\r\n");
        LED_FAIL = 1;
    }
}

/* ?????????????????????????????????????????????????????????????????????????????
 * Run continuity scan ? drive each J1 pin, report all J2 connections found
 * (original single-direction test kept for backward compatibility)
 * ????????????????????????????????????????????????????????????????????????????? */
static void run_test(void) {
    uint8_t  drv;
    uint16_t total_conn = 0u;

    g_i2c_error = 0u; g_i2c2_error = 0u;

    uart_puts("TEST_START,");
    uart_puts(g_serial);
    uart_puts("\r\n");

    for (drv = 0u; drv < TOTAL_PINS; drv++) {
        if (g_i2c_error) break;
        total_conn += scan_pin(drv);
    }

    if (g_i2c_error) {
        uart_puts("ERROR,I2C_TIMEOUT\r\n");
        LED_FAIL = 1;
        return;
    }

    uart_kn("TEST_DONE", total_conn);
    LED_PASS = 1; LED_FAIL = 0;
}

/* ?????????????????????????????????????????????????????????????????????????????
 * Diagnostic ? v2:
 *   1. Drive J1 pin drv_idx LOW via mux_a + za_drive_low
 *   2. Verify drive by reading LAT register directly (NOT za_read which undoes drive)
 *   3. Read back U19 OUT0 via I2C1 to confirm mux_a write reached the PCA9555
 *   4. Scan ALL 116 J2 pins ? report DIAG_CONN for every LOW result
 *   5. Read back U20 OUT0 via I2C2 after first mux_b to verify B-side write
 * ????????????????????????????????????????????????????????????????????????????? */
static void run_diag(uint8_t drv_idx, uint8_t ch_idx) {
    uint8_t  drv_mi, drv_ch;
    uint8_t  rp, rm, rc;
    uint8_t  val;
    uint8_t  rb19, rb20;
    uint16_t found;
    char     sa[6], sb[6];

    pin2mux(drv_idx, &drv_mi, &drv_ch);
    g_i2c_error = 0u; g_i2c2_error = 0u;
    found = 0u;

    uart_puts("DEBUG_START\r\n");

    /* ?? Step 1: Drive J1 pin LOW ??????????????????????????????????????????? */
    mux_a(drv_mi, drv_ch);
    za_drive_low(drv_mi);
    delay_us(500u);

    /* ?? Step 2: Verify drive via LAT (do NOT call za_read ? it switches pin  */
    /*           back to input and undoes the drive)                            */
    if (ZA_IS_RF[drv_mi])
        val = (uint8_t)((LATF  >> ZA_BIT[drv_mi]) & 0x01u);
    else
        val = (uint8_t)((LATE  >> ZA_BIT[drv_mi]) & 0x01u);
    uart_puts("ZA_LAT,");
    uart_putuint(val);   /* 0=LOW(good), 1=HIGH(za_drive_low failed) */
    uart_puts("\r\n");

    /* ?? Step 3: I2C1 readback of U19 OUT0 ??????????????????????????????????
     *    255 (0xFF) = write failed    112 (0x70) = correct for mux_a(0,0)     */
    rb19 = pca_read_reg(PCA_U19, PCA_OUT0);
    if (g_i2c_error) { uart_puts("DEBUG,I2C_READ_ERR_U19\r\n"); g_i2c_error = 0u; g_i2c2_error = 0u; }
    uart_puts("U19_OUT0,");
    uart_putuint(rb19);
    uart_puts("\r\n");

    /* ?? Step 4: Scan ALL 116 J2 pins ?????????????????????????????????????? */
    pin_name(drv_idx, sa);
    for (rp = 0u; rp < TOTAL_PINS; rp++) {
        if (g_i2c_error) break;
        pin2mux(rp, &rm, &rc);
        mux_b(rm, rc);

        /* After first mux_b, read back U20 OUT0 once via I2C2 */
        if (rp == 0u) {
            rb20 = pca2_read_reg(PCA_U20, PCA_OUT0);
            if (g_i2c_error) { uart_puts("DEBUG,I2C_READ_ERR_U20\r\n"); g_i2c_error = 0u; g_i2c2_error = 0u; }
            uart_puts("U20_OUT0,");
            uart_putuint(rb20);
            uart_puts("\r\n");
        }

        delay_us(100u);
        val = zb_read(rm);
        if (val == 0u) {
            j2pin_name(rp, sb);
            uart_puts("DIAG_CONN,");
            uart_puts(sa);
            uart_putc(',');
            uart_puts(sb);
            uart_puts("\r\n");
            found++;
        }
    }

    if (g_i2c_error) {
        uart_puts("DEBUG,I2C_ERROR\r\n");
        g_i2c_error = 0u; g_i2c2_error = 0u;
    }

    za_release(drv_mi);
    mux_off();
    uart_puts("DEBUG,CONN_FOUND,");
    uart_putuint(found);
    uart_puts("\r\n");
    uart_puts("DEBUG_DONE\r\n");
}

/* =============================================================
 * JIG SELF-DIAGNOSTIC  (command 'J')
 * ============================================================= */
static void cmd_jig_diag(void) {
    uint8_t  drv, mi, ch;
    uint8_t  rp, rm, rc;
    uint8_t  dbg_lat, dbg_out0, dbg_out1;
    char     sa[6];
    uint16_t found;
    uint8_t  fail_count;
    uint8_t  swept_mi;
    uint8_t  j2_fail_count;
    uint8_t  swept_mi_b;

    g_i2c_error  = 0u; g_i2c2_error = 0u;
    fail_count   = 0u;
    swept_mi     = 0u;

    uart_puts("JIG_DIAG_START\r\n");

    for (drv = 0u; drv < TOTAL_PINS; drv++) {

        pin2mux(drv, &mi, &ch);
        pin_name(drv, sa);

        /* ?? Drive J1 pin LOW ?????????????????????????????????? */
        mux_a(mi, ch);
        za_drive_low(mi);
        delay_us(200u);

        /* ?? Qu?t to?n b? J2 t?m continuity ??????????????????? */
        found = 0u;
        for (rp = 0u; rp < TOTAL_PINS; rp++) {
            if (g_i2c_error) break;
            pin2mux(rp, &rm, &rc);
            mux_b(rm, rc);
            delay_us(50u);
            if (zb_read(rm) == 0u) found++;
        }

        if (g_i2c_error) {
            uart_puts("JIG_DIAG_I2C_ERR,drv=");
            uart_putuint(drv);
            uart_puts("\r\n");
            g_i2c_error = 0u; g_i2c2_error = 0u;
            za_release(mi);
            mux_off();
            continue;
        }

        if (found > 0u) {
            uart_puts("DIAG_OK,");
            uart_puts(sa);
            uart_puts(",conn=");
            uart_putuint(found);
            uart_puts("\r\n");
        } else {
            fail_count++;

            dbg_lat  = ZA_IS_RF[mi]
                       ? (uint8_t)((LATF >> ZA_BIT[mi]) & 0x01u)
                       : (uint8_t)((LATE >> ZA_BIT[mi]) & 0x01u);
            dbg_out0 = pca_read_reg(PCA_U19, PCA_OUT0);   /* I2C1 */
            if (!g_i2c_error) {
                dbg_out1 = pca_read_reg(PCA_U19, PCA_OUT1);
            }

            uart_puts("DIAG_FAIL,");
            uart_puts(sa);
            uart_puts(",mi="); uart_putuint(mi);
            uart_puts(",ch="); uart_putuint(ch);
            uart_puts(",LAT="); uart_putuint(dbg_lat);
            if (g_i2c_error) {
                uart_puts(",I2C_ERR\r\n");
                g_i2c_error = 0u; g_i2c2_error = 0u;
            } else {
                uart_puts(",OUT0="); uart_putuint(dbg_out0);
                uart_puts(",OUT1="); uart_putuint(dbg_out1);
                uart_puts("\r\n");
            }

            if ((swept_mi & (uint8_t)(1u << mi)) == 0u) {
                uint8_t s_ch, s_rp, s_rm, s_rc;
                char    s_lbl[6];
                swept_mi |= (uint8_t)(1u << mi);

                uart_puts("SWEEP_START,mi=");
                uart_putuint(mi);
                uart_puts("\r\n");

                for (s_ch = 0u; s_ch < MUX_CH; s_ch++) {
                    mux_a(mi, s_ch);
                    za_drive_low(mi);
                    delay_us(300u);
                    for (s_rp = 0u; s_rp < TOTAL_PINS; s_rp++) {
                        if (g_i2c_error) break;
                        pin2mux(s_rp, &s_rm, &s_rc);
                        mux_b(s_rm, s_rc);
                        delay_us(150u);
                        if (zb_read(s_rm) == 0u) {
                            j2pin_name(s_rp, s_lbl);
                            uart_puts("SWEEP_HIT,ch=");
                            uart_putuint(s_ch);
                            uart_puts(",J2=");
                            uart_puts(s_lbl);
                            uart_puts("\r\n");
                        }
                    }
                    za_release(mi);
                    mux_off();
                    if (g_i2c_error) {
                        uart_puts("SWEEP_I2C_ERR,ch=");
                        uart_putuint(s_ch);
                        uart_puts("\r\n");
                        g_i2c_error = 0u; g_i2c2_error = 0u;
                        break;
                    }
                }

                uart_puts("SWEEP_END,mi=");
                uart_putuint(mi);
                uart_puts("\r\n");
            }
        }

        za_release(mi);
        mux_off();
    }

    /* ???????????????????????????????????????????????????????????
     * PASS 2 ? J2 side: drive m?i J2 pin LOW, qu?t J1 continuity
     * ??????????????????????????????????????????????????????????? */
    j2_fail_count = 0u;
    swept_mi_b    = 0u;

    uart_puts("JIG_DIAG_J2_START\r\n");

    for (drv = 0u; drv < TOTAL_PINS; drv++) {

        pin2mux(drv, &mi, &ch);

        /* ?? Drive J2 pin LOW via MUX-B + Z-line ??????????????? */
        mux_b(mi, ch);
        zb_drive_low(mi);
        delay_us(200u);

        /* ?? Qu?t to?n b? J1 t?m continuity ??????????????????? */
        found = 0u;
        rp = 0u;
        rm = 0u; rc = 0u;
        {
            uint8_t j1_hit = 0xFFu;
            for (rp = 0u; rp < TOTAL_PINS; rp++) {
                if (g_i2c_error) break;
                pin2mux(rp, &rm, &rc);
                mux_a(rm, rc);
                delay_us(50u);
                if (za_read(rm) == 0u) {
                    found++;
                    if (j1_hit == 0xFFu) j1_hit = rp;
                }
            }
            j2pin_name(drv, sa);   /* actual J2 driving pin */
        }

        if (g_i2c_error) {
            uart_puts("JIG_DIAG_J2_I2C_ERR,drv=");
            uart_putuint(drv);
            uart_puts("\r\n");
            g_i2c_error = 0u; g_i2c2_error = 0u;
            zb_release(mi);
            mux_off();
            continue;
        }

        if (found > 0u) {
            uart_puts("DIAG_J2_OK,");
            uart_puts(sa);
            uart_puts(",conn=");
            uart_putuint(found);
            uart_puts("\r\n");
        } else {
            j2_fail_count++;

            /* ??c l?i ZB LAT v? U20 OUT0/OUT1 via I2C2 */
            dbg_lat  = (uint8_t)((LATB >> ZB_BIT[mi]) & 0x01u);
            dbg_out0 = pca2_read_reg(PCA_U20, PCA_OUT0);   /* I2C2 */
            if (!g_i2c_error) {
                dbg_out1 = pca2_read_reg(PCA_U20, PCA_OUT1);
            }

            uart_puts("DIAG_J2_FAIL,");
            uart_puts(sa);
            uart_puts(",mi="); uart_putuint(mi);
            uart_puts(",ch="); uart_putuint(ch);
            uart_puts(",LAT="); uart_putuint(dbg_lat);
            if (g_i2c_error) {
                uart_puts(",I2C_ERR\r\n");
                g_i2c_error = 0u; g_i2c2_error = 0u;
            } else {
                uart_puts(",OUT0="); uart_putuint(dbg_out0);
                uart_puts(",OUT1="); uart_putuint(dbg_out1);
                uart_puts("\r\n");
            }

            if ((swept_mi_b & (uint8_t)(1u << mi)) == 0u) {
                uint8_t s_ch, s_rp, s_rm, s_rc;
                char    s_lbl[6];
                swept_mi_b |= (uint8_t)(1u << mi);

                uart_puts("SWEEP_J2_START,mi=");
                uart_putuint(mi);
                uart_puts("\r\n");

                for (s_ch = 0u; s_ch < MUX_CH; s_ch++) {
                    mux_b(mi, s_ch);
                    zb_drive_low(mi);
                    delay_us(300u);
                    for (s_rp = 0u; s_rp < TOTAL_PINS; s_rp++) {
                        if (g_i2c_error) break;
                        pin2mux(s_rp, &s_rm, &s_rc);
                        mux_a(s_rm, s_rc);
                        delay_us(150u);
                        if (za_read(s_rm) == 0u) {
                            pin_name(s_rp, s_lbl);
                            uart_puts("SWEEP_J2_HIT,ch=");
                            uart_putuint(s_ch);
                            uart_puts(",J1=");
                            uart_puts(s_lbl);
                            uart_puts("\r\n");
                        }
                    }
                    zb_release(mi);
                    mux_off();
                    if (g_i2c_error) {
                        uart_puts("SWEEP_J2_I2C_ERR,ch=");
                        uart_putuint(s_ch);
                        uart_puts("\r\n");
                        g_i2c_error = 0u; g_i2c2_error = 0u;
                        break;
                    }
                }

                uart_puts("SWEEP_J2_END,mi=");
                uart_putuint(mi);
                uart_puts("\r\n");
            }
        }

        zb_release(mi);
        mux_off();
    }

    uart_puts("JIG_DIAG_DONE,j1=");
    uart_putuint(fail_count);
    uart_puts(",j2=");
    uart_putuint(j2_fail_count);
    uart_puts("\r\n");
}

/* ?????????????????????????????????????????????????????????????????????????????
 * Process command from PC
 * ????????????????????????????????????????????????????????????????????????????? */
static void process_cmd(char *cmd) {
    uint8_t i;
    uint8_t j1, ch;
    uint8_t a;
    char   *src;

    if (cmd[0] == 'T') {
        g_testing = 1u;
        LED_BUSY = 1; LED_PASS = 0; LED_FAIL = 0;
        run_test();
        LED_BUSY = 0;
        g_testing = 0u;

    } else if (cmd[0] == 'X') {
        g_testing = 1u;
        LED_BUSY = 1; LED_PASS = 0; LED_FAIL = 0;
        run_test_bidir();
        LED_BUSY = 0;
        g_testing = 0u;

    } else if (cmd[0] == 'D') {
        j1 = 0u; ch = 0u;
        if (cmd[1] == ',') {
            src = cmd + 2;
            while (*src >= '0' && *src <= '9') j1 = (uint8_t)(j1 * 10u + (uint8_t)(*src++ - '0'));
            if (*src == ',') {
                src++;
                while (*src >= '0' && *src <= '9') ch = (uint8_t)(ch * 10u + (uint8_t)(*src++ - '0'));
            }
        }
        if (j1 >= TOTAL_PINS) j1 = 0u;
        if (ch >= MUX_CH)    ch = 0u;
        run_diag(j1, ch);

    } else if (cmd[0] == 'S' && cmd[1] == ',') {
        src = cmd + 2;
        i   = 0u;
        while (*src && i < 23u) g_serial[i++] = *src++;
        g_serial[i] = '\0';
        uart_puts("SERIAL,");
        uart_puts(g_serial);
        uart_puts("\r\n");

    } else if (cmd[0] == 'I') {
        /* I2C bus scan: probe addresses 0x20-0x27 on both buses */
        uart_puts("I2C_SCAN_START\r\n");
        uart_puts("BUS1,U19:\r\n");
        for (a = 0x20u; a <= 0x27u; a++) {
            uart_puts("I2C1_PROBE,0x");
            uart_puthex8(a);
            uart_putc(',');
            uart_puts(i2c_probe(a) ? "ACK" : "NACK");
            uart_puts("\r\n");
        }
        uart_puts("BUS2,U20:\r\n");
        for (a = 0x20u; a <= 0x27u; a++) {
            uart_puts("I2C2_PROBE,0x");
            uart_puthex8(a);
            uart_putc(',');
            uart_puts(i2c2_probe(a) ? "ACK" : "NACK");
            uart_puts("\r\n");
        }
        uart_puts("I2C_SCAN_DONE\r\n");

    } else if (cmd[0] == 'V') {
        uart_puts("VERSION," FW_VERSION "\r\n");

    } else if (cmd[0] == 'P') {
        uart_puts("PONG\r\n");

    } else if (cmd[0] == 'J') {
        cmd_jig_diag();

    } else if (cmd[0] == 'G') {
        {
            uint8_t rp_g, j1_g, mi_a, ch_a;
            char    sa_g[6];

            uart_puts("J2G0_START\r\n");
            for (rp_g = 0u; rp_g < 16u; rp_g++) {
                mux_b(0u, rp_g);
                zb_drive_low(0u);
                delay_us(200u);

                for (j1_g = 0u; j1_g < TOTAL_PINS; j1_g++) {
                    if (g_i2c_error) break;
                    pin2mux(j1_g, &mi_a, &ch_a);
                    mux_a(mi_a, ch_a);
                    delay_us(50u);
                    if (za_read(mi_a) == 0u) {
                        pin_name(j1_g, sa_g);
                        uart_puts("J2G0,rp=");
                        uart_putuint(rp_g);
                        uart_puts(",J1.");
                        uart_puts(sa_g);
                        uart_puts("\r\n");
                    }
                }

                zb_release(0u);
                mux_off();
                if (g_i2c_error) { g_i2c_error = 0u; g_i2c2_error = 0u; }
            }
            uart_puts("J2G0_END\r\n");
        }

    } else if (cmd[0] == 'F') {
        {
            uint8_t drv_f, rp_f, mi_a, ch_a, mi_b, ch_b;
            uint16_t hits;
            char     sa_f[6], sb_f[6];

            uart_puts("FDIAG_START\r\n");
            for (drv_f = 51u; drv_f <= 65u; drv_f += 2u) {
                pin2mux(drv_f, &mi_a, &ch_a);
                mux_a(mi_a, ch_a);
                za_drive_low(mi_a);
                delay_us(200u);
                pin_name(drv_f, sa_f);
                hits = 0u;

                for (rp_f = 0u; rp_f < TOTAL_PINS; rp_f++) {
                    if (g_i2c_error) break;
                    pin2mux(rp_f, &mi_b, &ch_b);
                    mux_b(mi_b, ch_b);
                    delay_us(50u);
                    if (zb_read(mi_b) == 0u) {
                        j2pin_name(rp_f, sb_f);
                        uart_puts("FDIAG,J1.");
                        uart_puts(sa_f);
                        uart_puts(",rp=");
                        uart_putuint(rp_f);
                        uart_puts(",J2.");
                        uart_puts(sb_f);
                        uart_puts("\r\n");
                        hits++;
                    }
                }

                if (hits == 0u) {
                    uart_puts("FDIAG,J1.");
                    uart_puts(sa_f);
                    uart_puts(",NO_CONN\r\n");
                }

                if (g_i2c_error) { g_i2c_error = 0u; g_i2c2_error = 0u; }
                za_release(mi_a);
                mux_off();
            }
            uart_puts("FDIAG_END\r\n");
        }
    }
}

/* ?????????????????????????????????????????????????????????????????????????????
 * Main
 * ????????????????????????????????????????????????????????????????????????????? */
int main(void) {
    uint8_t n;
    char    c;

    /* ?? Port init ?? */

    /* PORTB: B-side Z-lines (RB0-RB7, AN pins) + LED_PASS (RB8) */
    ADPCFG = 0xFFFFu;                            /* all RBx = digital */
    TRISB  = 0xFFFFu;                            /* all RB = input initially */
    TRISB &= (uint16_t)(~(1u << 8u));            /* RB8 = output (LED_PASS) */
    LATB  &= (uint16_t)(~(1u << 8u));            /* LED_PASS off */

    /* PORTE: A-side Z-lines (RE0-RE5) + BTN1 (RE8) ? all inputs */
    TRISE  = 0x01FFu;

    /* PORTF: A-side Z-lines A1(RF1) A5(RF0) ? all inputs (RF6=SDA2 managed by i2c2_init) */
    TRISF  = 0xFFFFu;

    /* PORTC: LED_FAIL(RC13) + LED_BUSY(RC14) ? outputs */
    TRISC &= (uint16_t)(~((1u << 13u) | (1u << 14u)));
    LATC  &= (uint16_t)(~((1u << 13u) | (1u << 14u)));  /* LEDs off */

    /* PORTD: BTN2(RD1)=input, LED_MODE(RD3)+LED_RDY(RD2)=outputs (RD0=SCL2 managed by i2c2_init) */
    TRISD &= (uint16_t)(~((1u << 2u) | (1u << 3u)));    /* RD2,RD3 output */
    LATD  &= (uint16_t)(~((1u << 2u) | (1u << 3u)));    /* LEDs off */

    /* ?? Peripheral init ?? */
    timer1_init();
    uart_init();
    delay_ms(10u);

    i2c_init();    /* I2C1 hardware ? U19 */
    i2c2_init();   /* I2C2 software ? U20 (RD0=SCL2, RF6=SDA2) */
    delay_ms(5u);

    pca_init();
    if (g_i2c_error) {
        uart_puts("ERROR,I2C_INIT_FAIL\r\n");
    }
    mux_off();

    delay_ms(200u);

    /* ?? Startup messages ?? */
    uart_puts("TESTER_READY\r\n");
    uart_puts("VERSION," FW_VERSION "\r\n");
    uart_kn("PINS", (uint16_t)TOTAL_PINS);

    /* Startup blink on all LEDs */
    for (n = 0u; n < 3u; n++) {
        LED_PASS = 1; LED_FAIL = 1; LED_BUSY = 1; LED_RDY = 1;
        delay_ms(150u);
        LED_PASS = 0; LED_FAIL = 0; LED_BUSY = 0; LED_RDY = 0;
        delay_ms(150u);
    }

    /* ?? Main loop ?? */
    while (1) {

        /* Poll UART2 for PC commands */
        if (U2STAbits.OERR) U2STAbits.OERR = 0u;   /* clear RX overflow */
        if (U2STAbits.URXDA) {
            c = (char)U2RXREG;
            if (c == '\r' || c == '\n') {
                if (rx_pos > 0u) {
                    rx_buf[rx_pos] = '\0';
                    process_cmd(rx_buf);
                    rx_pos = 0u;
                }
            } else if (rx_pos < 47u) {
                rx_buf[rx_pos++] = c;
            }
        }

        /* BTN1: start test */
        if (BTN1 == 0) {
            delay_ms(30u);
            if (BTN1 == 0) {
                process_cmd("T");
                while (BTN1 == 0);
                delay_ms(30u);
            }
        }

    }

    return 0;
}