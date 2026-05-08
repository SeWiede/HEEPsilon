/*
 * cgra_leftright_test — data passing between columns via RCL/RCR
 *
 * 2 columns (col_mask=0x3), row 0 only, k=6 instructions.
 *
 * Col 0 loads A and B, computes A*B, passes result rightward.
 * Col 1 loads C, reads col 0's result via RCL, computes (A*B)+C, stores it.
 *
 * SW reference: result = A*B + C
 *
 * Pipeline (RCL/RCR give previous cycle's ALU output from the neighbour):
 *
 *   PC  | Col 0                    | Col 1
 *   ----+--------------------------+----------------------------------
 *    0  | LWD→R0  (load A)        | LWD→R0  (load C)
 *    1  | LWD→R1  (load B)        | NOP
 *    2  | SMUL(R0,R1)→R2  (A*B)   | NOP
 *    3  | NOP                      | SADD(RCL,R0)→R1  (A*B + C)
 *         ^-- col1's RCL at PC3 = col0's PC2 output = A*B
 *    4  | NOP                      | SWD R1
 *    5  | EXIT                     | EXIT
 *
 * IMEM index: row 0, col c, pc p → imem[0*BKSZ + c*K + p]
 */

#include <stdio.h>
#include <string.h>
#include <stdint.h>

#include "csr.h"
#include "hart.h"
#include "handler.h"
#include "core_v_mini_mcu.h"
#include "rv_plic.h"
#include "rv_plic_regs.h"
#include "heepsilon.h"
#include "cgra.h"

#define PRINTF(fmt, ...) printf(fmt, ##__VA_ARGS__)

/* ── Instruction encoding ────────────────────────────────────────────────── */
#define INSTR(ma, mb, op, rs, we, fs, imm) \
    (  ((uint32_t)((ma)  & 0xF ) << 28) \
     | ((uint32_t)((mb)  & 0xF ) << 24) \
     | ((uint32_t)((op)  & 0x1F) << 19) \
     | ((uint32_t)((rs)  & 0x3 ) << 17) \
     | ((uint32_t)((we)  & 0x1 ) << 16) \
     | ((uint32_t)((fs)  & 0x7 ) << 13) \
     | ((uint32_t)((imm) & 0x1FFF)    ))

#define SRC_ZERO  0
#define SRC_RCL   2
#define SRC_R0    6
#define SRC_R1    7
#define SRC_R2    8

#define OP_SADD   1
#define OP_SMUL   3
#define OP_LWD    21
#define OP_SWD    22
#define OP_EXIT   25

#define KMEM_WORD(cols, start, n) \
    (((uint32_t)(cols) << 12) | ((uint32_t)(start) << 5) | ((uint32_t)(n) - 1))

#define K    6
#define BKSZ CGRA_CMEM_BK_DEPTH
#define II(col, pc)  ((col)*K + (pc))   /* row 0 only */

#define I_LWD_R0  INSTR(0,       0,      OP_LWD,  0, 1, 0, 4)
#define I_LWD_R1  INSTR(0,       0,      OP_LWD,  1, 1, 0, 4)
#define I_SWD_R1  INSTR(SRC_R1,  0,      OP_SWD,  0, 0, 0, 4)
#define I_EXIT    INSTR(0,       0,      OP_EXIT, 0, 0, 0, 0)
#define I_NOP     0

/* ── Globals ─────────────────────────────────────────────────────────────── */
static volatile int8_t cgra_intr_flag;
static cgra_t          cgra;

/* Col 0 reads {A, B}, col 1 reads {C} */
static int32_t in_col0[2] __attribute__((aligned(4)));
static int32_t in_col1[1] __attribute__((aligned(4)));
static int32_t dummy_out  __attribute__((aligned(4)));  /* col 0 write ptr */
static int32_t result     __attribute__((aligned(4)));  /* col 1 output    */

static uint32_t imem[CGRA_CMEM_TOT_DEPTH];
static uint32_t kmem[CGRA_KMEM_DEPTH];

void handler_irq_cgra(uint32_t id) { cgra_intr_flag = 1; }

/* ── Bitstream ───────────────────────────────────────────────────────────── */
static void build_bitstream(void)
{
    memset(imem, 0, sizeof(imem));
    memset(kmem, 0, sizeof(kmem));

    /* Col 0: load A→R0, load B→R1, multiply A*B→R2, then idle */
    imem[II(0, 0)] = I_LWD_R0;
    imem[II(0, 1)] = I_LWD_R1;
    imem[II(0, 2)] = INSTR(SRC_R0, SRC_R1, OP_SMUL, 2, 1, 0, 0); /* R2 = A*B */
    imem[II(0, 3)] = I_NOP;
    imem[II(0, 4)] = I_NOP;
    imem[II(0, 5)] = I_EXIT;

    /* Col 1: load C→R0, wait for col 0 to compute, then RCL+R0→R1, store */
    imem[II(1, 0)] = I_LWD_R0;                                     /* R0 = C  */
    imem[II(1, 1)] = I_NOP;
    imem[II(1, 2)] = I_NOP;
    imem[II(1, 3)] = INSTR(SRC_RCL, SRC_R0, OP_SADD, 1, 1, 0, 0); /* R1 = RCL(=A*B) + R0(=C) */
    imem[II(1, 4)] = I_SWD_R1;
    imem[II(1, 5)] = I_EXIT;

    /* Kernel 1: cols 0+1 active (mask=0x3), start=0, 6 instructions */
    kmem[1] = KMEM_WORD(0x3, 0, K);
}

/* ── Kernel runner ───────────────────────────────────────────────────────── */
static void run_kernel(int kid)
{
    cgra_set_read_ptr (&cgra, (uint32_t)in_col0,   0);
    cgra_set_write_ptr(&cgra, (uint32_t)&dummy_out, 0);
    cgra_set_read_ptr (&cgra, (uint32_t)in_col1,   1);
    cgra_set_write_ptr(&cgra, (uint32_t)&result,    1);
    cgra_intr_flag = 0;
    cgra_set_kernel(&cgra, kid);
    while (!cgra_intr_flag)
        wait_for_interrupt();
}

/* ── main ────────────────────────────────────────────────────────────────── */
int main(void)
{
    plic_Init();
    plic_irq_set_priority(CGRA_INTR, 1);
    plic_irq_set_enabled(CGRA_INTR, kPlicToggleEnabled);
    plic_assign_external_irq_handler(CGRA_INTR, &handler_irq_cgra);
    CSR_SET_BITS(CSR_REG_MSTATUS, 0x8);
    CSR_SET_BITS(CSR_REG_MIE, 1 << 11);
    cgra_intr_flag = 0;
    cgra.base_addr = mmio_region_from_addr((uintptr_t)CGRA_PERIPH_START_ADDRESS);

    /* Test values */
    const int32_t A = 3, B = 4, C = 10;
    in_col0[0] = A;
    in_col0[1] = B;
    in_col1[0] = C;

    const int32_t expected = A * B + C;  /* 3*4+10 = 22 */

    build_bitstream();
    cgra_cmem_init(imem, kmem);
    run_kernel(1);

    PRINTF("=== CGRA left-right test ===\n");
    PRINTF("A=%d B=%d C=%d\n", (int)A, (int)B, (int)C);
    PRINTF("expected A*B+C = %d\n", (int)expected);
    PRINTF("CGRA result:   %d\n", (int)result);

    int ok = (result == expected);
    PRINTF("Left-right pass %s\n", ok ? "PASS" : "FAIL");
    PRINTF("\nfinished with %d errors\n", ok ? 0 : 1);
    PRINTF("### DONE ###\n");
    return ok ? 0 : 1;
}
