/*
 * cgra_fullgrid_test — all 16 RCs (4×4) active, each computing a distinct function
 *
 * 4 columns × 4 rows, K=6 instructions, col_mask=0xF.
 *
 * Col 0 (arithmetic):     Row0=A+B,   Row1=A-B,   Row2=A×B,    Row3=(A+B)/2
 * Col 1 (power/abs):      Row0=N²,    Row1=2N,    Row2=N²+N,   Row3=|N|
 * Col 2 (bitwise):        Row0=A&B,   Row1=A|B,   Row2=A^B,    Row3=~(A&B)
 * Col 3 (fused mul):      Row0=A×B+A, Row1=A×B-B, Row2=A²+B,   Row3=2A+B
 *
 * Memory layout (LWD is per-row sequential, stride=4 bytes):
 *   Col 0/2/3 inputs: [A0,A1,A2,A3, B0,B1,B2,B3]  — 8 words
 *   Col 1 inputs:     [N0,N1,N2,N3]                — 4 words
 *   All columns: 4 output words, stored by all rows at the same PC step
 *     so out[row] = result for that row (natural row order).
 *
 * Pipeline timing (all rows advance PC together; SMUL stalls whole column):
 *
 *   Col 0:
 *     PC0: LWD→R0 (Ar)
 *     PC1: LWD→R1 (Br)
 *     PC2: SADD/SSUB/SMUL[stall]/SADD → R2
 *     PC3: NOP/NOP/NOP/SRA(R2,1)→R3
 *     PC4: SWD R2/R2/R2/R3
 *     PC5: EXIT
 *
 *   Col 1:
 *     PC0: LWD→R0 (Nr)
 *     PC1: SMUL[stall]/SADD/SMUL[stall]/SABS → R1
 *     PC2: NOP/NOP/SADD(R1,R0)→R2/NOP
 *     PC3: SWD R1/R1/R2/R1
 *     PC4: NOP
 *     PC5: EXIT
 *
 *   Col 2:
 *     PC0: LWD→R0 (Ar)
 *     PC1: LWD→R1 (Br)
 *     PC2: LAND/LOR/LXOR/LNAND → R2
 *     PC3: SWD R2
 *     PC4: NOP
 *     PC5: EXIT
 *
 *   Col 3:
 *     PC0: LWD→R0 (Ar)
 *     PC1: LWD→R1 (Br)
 *     PC2: SMUL/SMUL/SMUL[stall]/SADD → R2
 *     PC3: SADD(R2,R0)/SSUB(R2,R1)/SADD(R2,R1)/SADD(R2,R1) → R3
 *     PC4: SWD R3
 *     PC5: EXIT
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

/* Operand source indices */
#define SRC_ZERO  0
#define SRC_R0    6
#define SRC_R1    7
#define SRC_R2    8
#define SRC_R3    9
#define SRC_IMM   10

/* ALU opcodes (cgra_pkg.sv) */
#define OP_SADD   1
#define OP_SSUB   2
#define OP_SMUL   3
#define OP_SRA    7
#define OP_LAND   8
#define OP_LOR    9
#define OP_LXOR   10
#define OP_LNAND  11
#define OP_LWD    21
#define OP_SWD    22
#define OP_EXIT   25
#define OP_SABS   26

/* KMEM: [15:12]=col_mask [11:5]=start_addr [4:0]=(n_instr-1) */
#define KMEM_WORD(cols, start, n) \
    (((uint32_t)(cols) << 12) | ((uint32_t)(start) << 5) | ((uint32_t)(n) - 1))

#define K    6
#define BKSZ CGRA_CMEM_BK_DEPTH
/* IMEM flat index: row r, col c, pc p  (start_add=0) */
#define II(r, c, p)  ((r)*BKSZ + (c)*K + (p))

/* Common instruction shorthands */
#define I_LWD_R0   INSTR(0,       0,      OP_LWD,  0, 1, 0, 4)
#define I_LWD_R1   INSTR(0,       0,      OP_LWD,  1, 1, 0, 4)
#define I_SWD_R1   INSTR(SRC_R1,  0,      OP_SWD,  0, 0, 0, 4)
#define I_SWD_R2   INSTR(SRC_R2,  0,      OP_SWD,  0, 0, 0, 4)
#define I_SWD_R3   INSTR(SRC_R3,  0,      OP_SWD,  0, 0, 0, 4)
#define I_EXIT     INSTR(0,       0,      OP_EXIT, 0, 0, 0, 0)
#define I_NOP      0

/* ── Globals ─────────────────────────────────────────────────────────────── */
static volatile int8_t cgra_intr_flag;
static cgra_t          cgra;

/* Col 0/2/3: 8 inputs [A0..A3, B0..B3]; Col 1: 4 inputs [N0..N3] */
static int32_t in_col0[8] __attribute__((aligned(4)));
static int32_t in_col1[4] __attribute__((aligned(4)));
static int32_t in_col2[8] __attribute__((aligned(4)));
static int32_t in_col3[8] __attribute__((aligned(4)));

/* 4 outputs per column, written in row order at a single SWD PC step */
static int32_t out_col0[4] __attribute__((aligned(4)));
static int32_t out_col1[4] __attribute__((aligned(4)));
static int32_t out_col2[4] __attribute__((aligned(4)));
static int32_t out_col3[4] __attribute__((aligned(4)));

static uint32_t imem[CGRA_CMEM_TOT_DEPTH];
static uint32_t kmem[CGRA_KMEM_DEPTH];

void handler_irq_cgra(uint32_t id) { cgra_intr_flag = 1; }

/* ── Bitstream ───────────────────────────────────────────────────────────── */
static void build_bitstream(void)
{
    memset(imem, 0, sizeof(imem));
    memset(kmem, 0, sizeof(kmem));

    /* ════════════════════════════════════════════════════════════════════
     * COL 0  —  A+B | A−B | A×B | (A+B)/2
     * inputs : in_col0[8]  = [A0..A3, B0..B3]
     * outputs: out_col0[4] = [A+B, A−B, A×B, (A+B)/2]
     * ════════════════════════════════════════════════════════════════════ */

    /* Row 0: A+B */
    imem[II(0,0,0)] = I_LWD_R0;
    imem[II(0,0,1)] = I_LWD_R1;
    imem[II(0,0,2)] = INSTR(SRC_R0, SRC_R1, OP_SADD, 2, 1, 0, 0); /* R2 = A+B */
    imem[II(0,0,3)] = I_NOP;
    imem[II(0,0,4)] = I_SWD_R2;
    imem[II(0,0,5)] = I_EXIT;

    /* Row 1: A−B */
    imem[II(1,0,0)] = I_LWD_R0;
    imem[II(1,0,1)] = I_LWD_R1;
    imem[II(1,0,2)] = INSTR(SRC_R0, SRC_R1, OP_SSUB, 2, 1, 0, 0); /* R2 = A−B */
    imem[II(1,0,3)] = I_NOP;
    imem[II(1,0,4)] = I_SWD_R2;
    imem[II(1,0,5)] = I_EXIT;

    /* Row 2: A×B (SMUL stalls whole column at PC2) */
    imem[II(2,0,0)] = I_LWD_R0;
    imem[II(2,0,1)] = I_LWD_R1;
    imem[II(2,0,2)] = INSTR(SRC_R0, SRC_R1, OP_SMUL, 2, 1, 0, 0); /* R2 = A×B */
    imem[II(2,0,3)] = I_NOP;
    imem[II(2,0,4)] = I_SWD_R2;
    imem[II(2,0,5)] = I_EXIT;

    /* Row 3: (A+B)/2  — add, then arithmetic right-shift by 1 */
    imem[II(3,0,0)] = I_LWD_R0;
    imem[II(3,0,1)] = I_LWD_R1;
    imem[II(3,0,2)] = INSTR(SRC_R0, SRC_R1, OP_SADD, 2, 1, 0, 0); /* R2 = A+B  */
    imem[II(3,0,3)] = INSTR(SRC_R2, SRC_IMM, OP_SRA,  3, 1, 0, 1); /* R3 = R2>>1 */
    imem[II(3,0,4)] = I_SWD_R3;
    imem[II(3,0,5)] = I_EXIT;

    /* ════════════════════════════════════════════════════════════════════
     * COL 1  —  N² | 2N | N²+N | |N|
     * inputs : in_col1[4]  = [N0..N3]
     * outputs: out_col1[4] = [N², 2N, N²+N, |N|]
     * SMUL rows 0 & 2 stall column at PC1
     * ════════════════════════════════════════════════════════════════════ */

    /* Row 0: N² */
    imem[II(0,1,0)] = I_LWD_R0;
    imem[II(0,1,1)] = INSTR(SRC_R0, SRC_R0, OP_SMUL, 1, 1, 0, 0); /* R1 = N² */
    imem[II(0,1,2)] = I_NOP;
    imem[II(0,1,3)] = I_SWD_R1;
    imem[II(0,1,4)] = I_NOP;
    imem[II(0,1,5)] = I_EXIT;

    /* Row 1: 2N */
    imem[II(1,1,0)] = I_LWD_R0;
    imem[II(1,1,1)] = INSTR(SRC_R0, SRC_R0, OP_SADD, 1, 1, 0, 0); /* R1 = 2N */
    imem[II(1,1,2)] = I_NOP;
    imem[II(1,1,3)] = I_SWD_R1;
    imem[II(1,1,4)] = I_NOP;
    imem[II(1,1,5)] = I_EXIT;

    /* Row 2: N²+N  (SMUL stalls column at PC1; R0 still valid at PC2) */
    imem[II(2,1,0)] = I_LWD_R0;
    imem[II(2,1,1)] = INSTR(SRC_R0, SRC_R0, OP_SMUL, 1, 1, 0, 0); /* R1 = N² */
    imem[II(2,1,2)] = INSTR(SRC_R1, SRC_R0, OP_SADD, 2, 1, 0, 0); /* R2 = N²+N */
    imem[II(2,1,3)] = I_SWD_R2;
    imem[II(2,1,4)] = I_NOP;
    imem[II(2,1,5)] = I_EXIT;

    /* Row 3: |N|  (SABS: result = (muxA < 0) ? −muxA : muxA) */
    imem[II(3,1,0)] = I_LWD_R0;
    imem[II(3,1,1)] = INSTR(SRC_R0, 0,      OP_SABS, 1, 1, 0, 0); /* R1 = |N| */
    imem[II(3,1,2)] = I_NOP;
    imem[II(3,1,3)] = I_SWD_R1;
    imem[II(3,1,4)] = I_NOP;
    imem[II(3,1,5)] = I_EXIT;

    /* ════════════════════════════════════════════════════════════════════
     * COL 2  —  A&B | A|B | A^B | ~(A&B)
     * inputs : in_col2[8]  = [A0..A3, B0..B3]
     * outputs: out_col2[4] = [A&B, A|B, A^B, ~(A&B)]
     * ════════════════════════════════════════════════════════════════════ */

    /* Row 0: A & B */
    imem[II(0,2,0)] = I_LWD_R0;
    imem[II(0,2,1)] = I_LWD_R1;
    imem[II(0,2,2)] = INSTR(SRC_R0, SRC_R1, OP_LAND,  2, 1, 0, 0);
    imem[II(0,2,3)] = I_SWD_R2;
    imem[II(0,2,4)] = I_NOP;
    imem[II(0,2,5)] = I_EXIT;

    /* Row 1: A | B */
    imem[II(1,2,0)] = I_LWD_R0;
    imem[II(1,2,1)] = I_LWD_R1;
    imem[II(1,2,2)] = INSTR(SRC_R0, SRC_R1, OP_LOR,   2, 1, 0, 0);
    imem[II(1,2,3)] = I_SWD_R2;
    imem[II(1,2,4)] = I_NOP;
    imem[II(1,2,5)] = I_EXIT;

    /* Row 2: A ^ B */
    imem[II(2,2,0)] = I_LWD_R0;
    imem[II(2,2,1)] = I_LWD_R1;
    imem[II(2,2,2)] = INSTR(SRC_R0, SRC_R1, OP_LXOR,  2, 1, 0, 0);
    imem[II(2,2,3)] = I_SWD_R2;
    imem[II(2,2,4)] = I_NOP;
    imem[II(2,2,5)] = I_EXIT;

    /* Row 3: ~(A & B) */
    imem[II(3,2,0)] = I_LWD_R0;
    imem[II(3,2,1)] = I_LWD_R1;
    imem[II(3,2,2)] = INSTR(SRC_R0, SRC_R1, OP_LNAND, 2, 1, 0, 0);
    imem[II(3,2,3)] = I_SWD_R2;
    imem[II(3,2,4)] = I_NOP;
    imem[II(3,2,5)] = I_EXIT;

    /* ════════════════════════════════════════════════════════════════════
     * COL 3  —  A×B+A | A×B−B | A²+B | 2A+B
     * inputs : in_col3[8]  = [A0..A3, B0..B3]
     * outputs: out_col3[4] = [A×B+A, A×B−B, A²+B, 2A+B]
     * SMUL rows 0,1,2 stall column at PC2; row 3 (SADD) waits with them
     * ════════════════════════════════════════════════════════════════════ */

    /* Row 0: A×B+A */
    imem[II(0,3,0)] = I_LWD_R0;
    imem[II(0,3,1)] = I_LWD_R1;
    imem[II(0,3,2)] = INSTR(SRC_R0, SRC_R1, OP_SMUL, 2, 1, 0, 0); /* R2 = A×B */
    imem[II(0,3,3)] = INSTR(SRC_R2, SRC_R0, OP_SADD, 3, 1, 0, 0); /* R3 = A×B+A */
    imem[II(0,3,4)] = I_SWD_R3;
    imem[II(0,3,5)] = I_EXIT;

    /* Row 1: A×B−B */
    imem[II(1,3,0)] = I_LWD_R0;
    imem[II(1,3,1)] = I_LWD_R1;
    imem[II(1,3,2)] = INSTR(SRC_R0, SRC_R1, OP_SMUL, 2, 1, 0, 0); /* R2 = A×B */
    imem[II(1,3,3)] = INSTR(SRC_R2, SRC_R1, OP_SSUB, 3, 1, 0, 0); /* R3 = A×B−B */
    imem[II(1,3,4)] = I_SWD_R3;
    imem[II(1,3,5)] = I_EXIT;

    /* Row 2: A²+B */
    imem[II(2,3,0)] = I_LWD_R0;
    imem[II(2,3,1)] = I_LWD_R1;
    imem[II(2,3,2)] = INSTR(SRC_R0, SRC_R0, OP_SMUL, 2, 1, 0, 0); /* R2 = A² */
    imem[II(2,3,3)] = INSTR(SRC_R2, SRC_R1, OP_SADD, 3, 1, 0, 0); /* R3 = A²+B */
    imem[II(2,3,4)] = I_SWD_R3;
    imem[II(2,3,5)] = I_EXIT;

    /* Row 3: 2A+B */
    imem[II(3,3,0)] = I_LWD_R0;
    imem[II(3,3,1)] = I_LWD_R1;
    imem[II(3,3,2)] = INSTR(SRC_R0, SRC_R0, OP_SADD, 2, 1, 0, 0); /* R2 = 2A */
    imem[II(3,3,3)] = INSTR(SRC_R2, SRC_R1, OP_SADD, 3, 1, 0, 0); /* R3 = 2A+B */
    imem[II(3,3,4)] = I_SWD_R3;
    imem[II(3,3,5)] = I_EXIT;

    /* Kernel 1: all 4 cols (mask=0xF), start=0, 6 instructions */
    kmem[1] = KMEM_WORD(0xF, 0, K);
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

    /* ── Test values ─────────────────────────────────────────────────── */

    /* Col 0: A+B=7, A−B=3, A×B=6, (A+B)/2=4 */
    const int32_t c0A[4] = { 3,  5, 2, 6 };
    const int32_t c0B[4] = { 4,  2, 3, 2 };

    /* Col 1: N²=9, 2N=8, N²+N=20, |N|=7 */
    const int32_t c1N[4] = { -3,  4, -5, -7 };

    /* Col 2: A&B=0x03, A|B=0xFF, A^B=0xA5, ~(A&B)=~0x50 */
    const int32_t c2A[4] = { 0x0F, 0xFF, 0xAA, 0xF0 };
    const int32_t c2B[4] = { 0x33, 0x55, 0x0F, 0x5A };

    /* Col 3: A×B+A=8, A×B−B=8, A²+B=21, 2A+B=16 */
    const int32_t c3A[4] = { 2, 3, 4, 5 };
    const int32_t c3B[4] = { 3, 4, 5, 6 };

    /* Fill input buffers */
    for (int r = 0; r < 4; r++) {
        in_col0[r]   = c0A[r];  in_col0[4+r] = c0B[r];
        in_col1[r]   = c1N[r];
        in_col2[r]   = c2A[r];  in_col2[4+r] = c2B[r];
        in_col3[r]   = c3A[r];  in_col3[4+r] = c3B[r];
    }

    /* SW reference */
    const int32_t exp0[4] = {
        c0A[0] + c0B[0],                       /*  3+ 4 =  7 */
        c0A[1] - c0B[1],                       /*  5− 2 =  3 */
        c0A[2] * c0B[2],                       /*  2× 3 =  6 */
        (c0A[3] + c0B[3]) >> 1,               /* (6+2)>>1 = 4 */
    };
    const int32_t exp1[4] = {
        c1N[0] * c1N[0],                       /* (−3)²   =  9 */
        2 * c1N[1],                            /* 2×4     =  8 */
        c1N[2] * c1N[2] + c1N[2],             /* 25+(−5) = 20 */
        c1N[3] < 0 ? -c1N[3] : c1N[3],        /* |−7|    =  7 */
    };
    const int32_t exp2[4] = {
        c2A[0] & c2B[0],                       /* 0x0F&0x33 = 0x03 */
        c2A[1] | c2B[1],                       /* 0xFF|0x55 = 0xFF */
        c2A[2] ^ c2B[2],                       /* 0xAA^0x0F = 0xA5 */
        ~(c2A[3] & c2B[3]),                    /* ~(0xF0&0x5A) = ~0x50 */
    };
    const int32_t exp3[4] = {
        c3A[0] * c3B[0] + c3A[0],             /* 2×3+2  =  8 */
        c3A[1] * c3B[1] - c3B[1],             /* 3×4−4  =  8 */
        c3A[2] * c3A[2] + c3B[2],             /* 4²+5   = 21 */
        2 * c3A[3] + c3B[3],                  /* 2×5+6  = 16 */
    };

    /* ── Build and load bitstream ───────────────────────────────────── */
    build_bitstream();
    cgra_cmem_init(imem, kmem);

    /* ── Set per-column pointers and launch ─────────────────────────── */
    cgra_set_read_ptr (&cgra, (uint32_t)in_col0,  0);
    cgra_set_write_ptr(&cgra, (uint32_t)out_col0, 0);
    cgra_set_read_ptr (&cgra, (uint32_t)in_col1,  1);
    cgra_set_write_ptr(&cgra, (uint32_t)out_col1, 1);
    cgra_set_read_ptr (&cgra, (uint32_t)in_col2,  2);
    cgra_set_write_ptr(&cgra, (uint32_t)out_col2, 2);
    cgra_set_read_ptr (&cgra, (uint32_t)in_col3,  3);
    cgra_set_write_ptr(&cgra, (uint32_t)out_col3, 3);

    cgra_intr_flag = 0;
    cgra_set_kernel(&cgra, 1);
    while (!cgra_intr_flag)
        wait_for_interrupt();

    /* ── Check results ──────────────────────────────────────────────── */
    PRINTF("=== CGRA fullgrid test (4×4 = 16 RCs) ===\n");

    int errors = 0;

    /* Col 0 */
    const char *c0_names[4] = { "A+B", "A-B", "A*B", "(A+B)/2" };
    PRINTF("Col 0 (arithmetic):\n");
    for (int r = 0; r < 4; r++) {
        int ok = (out_col0[r] == exp0[r]);
        PRINTF("  Row%d %-8s : got %d, expected %d  %s\n",
               r, c0_names[r], (int)out_col0[r], (int)exp0[r], ok ? "PASS" : "FAIL");
        if (!ok) errors++;
    }

    /* Col 1 */
    const char *c1_names[4] = { "N^2", "2N", "N^2+N", "|N|" };
    PRINTF("Col 1 (power/abs):\n");
    for (int r = 0; r < 4; r++) {
        int ok = (out_col1[r] == exp1[r]);
        PRINTF("  Row%d %-8s : got %d, expected %d  %s\n",
               r, c1_names[r], (int)out_col1[r], (int)exp1[r], ok ? "PASS" : "FAIL");
        if (!ok) errors++;
    }

    /* Col 2 */
    const char *c2_names[4] = { "A&B", "A|B", "A^B", "~(A&B)" };
    PRINTF("Col 2 (bitwise):\n");
    for (int r = 0; r < 4; r++) {
        int ok = (out_col2[r] == exp2[r]);
        PRINTF("  Row%d %-8s : got 0x%08X, expected 0x%08X  %s\n",
               r, c2_names[r], (unsigned)out_col2[r], (unsigned)exp2[r], ok ? "PASS" : "FAIL");
        if (!ok) errors++;
    }

    /* Col 3 */
    const char *c3_names[4] = { "A*B+A", "A*B-B", "A^2+B", "2A+B" };
    PRINTF("Col 3 (fused mul):\n");
    for (int r = 0; r < 4; r++) {
        int ok = (out_col3[r] == exp3[r]);
        PRINTF("  Row%d %-8s : got %d, expected %d  %s\n",
               r, c3_names[r], (int)out_col3[r], (int)exp3[r], ok ? "PASS" : "FAIL");
        if (!ok) errors++;
    }

    PRINTF("\nfinished with %d errors\n", errors);
    return errors ? 1 : 0;
}
