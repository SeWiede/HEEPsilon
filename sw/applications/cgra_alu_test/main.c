/*
 * cgra_alu_test — tests every CGRA ALU operation
 *
 * Single cgra_cmem_init loads five kernels into IMEM row-0.
 * All kernels use column 0 only (col_mask=0x1) — no multi-column stall.
 * LWD/SWD with explicit read/write ptr set before each kernel run.
 *
 * IMEM row-0 layout (93 instructions, fits in 128-slot bank):
 *   [ 0.. 5] K5 (ID 5): JUMP
 *   [ 6..25] K1 (ID 1): SADD SSUB SMUL FXPMUL SABS
 *   [26..54] K2 (ID 2): SLL SRL SRA LAND LOR LXOR LNAND
 *   [55..75] K3 (ID 3): LNOR LNXOR BSFA BZFA
 *   [76..92] K4 (ID 4): BEQ BNE BLT BGE (not-taken cases)
 *
 * Execution order: K5, K1, K2, K3, K4
 * output_buf: [0]=JUMP [1..5]=K1 [6..12]=K2 [13..16]=K3 [17..20]=K4
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

/* ── Instruction encoding ──────────────────────────────────────────────── */
#define INSTR(ma, mb, op, rs, we, fs, imm) \
    (  ((uint32_t)((ma)  & 0xF ) << 28) \
     | ((uint32_t)((mb)  & 0xF ) << 24) \
     | ((uint32_t)((op)  & 0x1F) << 19) \
     | ((uint32_t)((rs)  & 0x3 ) << 17) \
     | ((uint32_t)((we)  & 0x1 ) << 16) \
     | ((uint32_t)((fs)  & 0x7 ) << 13) \
     | ((uint32_t)((imm) & 0x1FFF)    ))

#define SRC_ZERO  0
#define SRC_R0    6
#define SRC_R1    7
#define SRC_R2    8
#define SRC_IMM   10

#define OP_SADD   1
#define OP_SSUB   2
#define OP_SMUL   3
#define OP_FXPMUL 4
#define OP_SLL    5
#define OP_SRL    6
#define OP_SRA    7
#define OP_LAND   8
#define OP_LOR    9
#define OP_LXOR   10
#define OP_LNAND  11
#define OP_LNOR   12
#define OP_LNXOR  13
#define OP_BSFA   14
#define OP_BZFA   15
#define OP_BEQ    16
#define OP_BNE    17
#define OP_BLT    18
#define OP_BGE    19
#define OP_JUMP   20
#define OP_LWD    21
#define OP_SWD    22
#define OP_EXIT   25
#define OP_SABS   26

/* KMEM word: [15:12]=col_mask [11:5]=start_addr [4:0]=(n_instr-1) */
#define KMEM_WORD(cols, start, n) \
    (((uint32_t)(cols) << 12) | ((uint32_t)(start) << 5) | ((uint32_t)(n) - 1))
/* Col-0 only kernel */
#define KW1(start, n) KMEM_WORD(0x1, (start), (n))

/* Instruction shorthands */
#define I_LWD_R0  INSTR(0, 0, OP_LWD, 0, 1, 0, 4)   /* load → R0, stride=4 */
#define I_LWD_R1  INSTR(0, 0, OP_LWD, 1, 1, 0, 4)   /* load → R1, stride=4 */
#define I_LWD_R2  INSTR(0, 0, OP_LWD, 2, 1, 0, 4)   /* load → R2, stride=4 */
#define I_SWD_R1  INSTR(SRC_R1, 0, OP_SWD, 0, 0, 0, 4) /* store R1, stride=4 */
#define I_SWD_R2  INSTR(SRC_R2, 0, OP_SWD, 0, 0, 0, 4) /* store R2, stride=4 */
#define I_EXIT    INSTR(0, 0, OP_EXIT, 0, 0, 0, 0)
/* Two-operand op: OP(R0,R1)→R2 */
#define I_2OP(op) INSTR(SRC_R0, SRC_R1, (op), 2, 1, 0, 0)

/* ── Globals ───────────────────────────────────────────────────────────── */

static volatile int8_t cgra_intr_flag;
static cgra_t          cgra;

/* 42 inputs and 21 outputs for col0 */
static int32_t  input_buf [42] __attribute__((aligned(4)));
static int32_t  output_buf[21] __attribute__((aligned(4)));

static uint32_t imem[CGRA_CMEM_TOT_DEPTH];
static uint32_t kmem[CGRA_KMEM_DEPTH];

void handler_irq_cgra(uint32_t id) { cgra_intr_flag = 1; }

static int32_t fxp_ref(int32_t a, int32_t b) {
    return (int32_t)(((int64_t)a * b) >> 15);
}

/* ── Bitstream ─────────────────────────────────────────────────────────── */

static void build_bitstream(void)
{
    memset(imem, 0, sizeof(imem));
    memset(kmem, 0, sizeof(kmem));

    int p = 0; /* IMEM write cursor */

    /* ── K5: JUMP (start=0, n=6) ─────────────────────────────────────── *
     * conf PC0: LWD→R0
     * conf PC1: SADD(R0,0)→R1    R1 = input value
     * conf PC2: JUMP(imm=4)       jump to conf PC4, skip PC3
     * conf PC3: SSUB(R1,R1)→R1   R1=0 — only reached if JUMP fails
     * conf PC4: SWD R1
     * conf PC5: EXIT
     */
    imem[p++] = I_LWD_R0;
    imem[p++] = INSTR(SRC_R0, SRC_ZERO, OP_SADD, 1, 1, 0, 0);
    imem[p++] = INSTR(SRC_ZERO, SRC_IMM, OP_JUMP, 0, 0, 0, 4);
    imem[p++] = INSTR(SRC_R1, SRC_R1, OP_SSUB, 1, 1, 0, 0);
    imem[p++] = I_SWD_R1;
    imem[p++] = I_EXIT;      /* p == 6 */
    kmem[5]   = KW1(0, 6);

    /* ── K1: SADD SSUB SMUL FXPMUL SABS (start=6, n=20) ──────────────── */
    imem[p++]=I_LWD_R0; imem[p++]=I_LWD_R1; imem[p++]=I_2OP(OP_SADD);   imem[p++]=I_SWD_R2;
    imem[p++]=I_LWD_R0; imem[p++]=I_LWD_R1; imem[p++]=I_2OP(OP_SSUB);   imem[p++]=I_SWD_R2;
    imem[p++]=I_LWD_R0; imem[p++]=I_LWD_R1; imem[p++]=I_2OP(OP_SMUL);   imem[p++]=I_SWD_R2;
    imem[p++]=I_LWD_R0; imem[p++]=I_LWD_R1; imem[p++]=I_2OP(OP_FXPMUL); imem[p++]=I_SWD_R2;
    imem[p++]=I_LWD_R0;
    imem[p++]=INSTR(SRC_R0, SRC_ZERO, OP_SABS, 1, 1, 0, 0);
    imem[p++]=I_SWD_R1;
    imem[p++]=I_EXIT;    /* p == 26 */
    kmem[1]   = KW1(6, 20);

    /* ── K2: SLL SRL SRA LAND LOR LXOR LNAND (start=26, n=29) ─────────── */
    imem[p++]=I_LWD_R0; imem[p++]=I_LWD_R1; imem[p++]=I_2OP(OP_SLL);   imem[p++]=I_SWD_R2;
    imem[p++]=I_LWD_R0; imem[p++]=I_LWD_R1; imem[p++]=I_2OP(OP_SRL);   imem[p++]=I_SWD_R2;
    imem[p++]=I_LWD_R0; imem[p++]=I_LWD_R1; imem[p++]=I_2OP(OP_SRA);   imem[p++]=I_SWD_R2;
    imem[p++]=I_LWD_R0; imem[p++]=I_LWD_R1; imem[p++]=I_2OP(OP_LAND);  imem[p++]=I_SWD_R2;
    imem[p++]=I_LWD_R0; imem[p++]=I_LWD_R1; imem[p++]=I_2OP(OP_LOR);   imem[p++]=I_SWD_R2;
    imem[p++]=I_LWD_R0; imem[p++]=I_LWD_R1; imem[p++]=I_2OP(OP_LXOR);  imem[p++]=I_SWD_R2;
    imem[p++]=I_LWD_R0; imem[p++]=I_LWD_R1; imem[p++]=I_2OP(OP_LNAND); imem[p++]=I_SWD_R2;
    imem[p++]=I_EXIT;    /* p == 55 */
    kmem[2]   = KW1(26, 29);

    /* ── K3: LNOR LNXOR BSFA BZFA (start=55, n=21) ─────────────────────
     * BSFA: LWD_R0(cond) LWD_R1(tval) LWD_R2(fval)
     *       SADD(R0,0)→R0  (sets sign/zero flags for next cycle)
     *       BSFA(R1,R2)→R1 (reads registered flags; selects R1 if sign, else R2)
     *       SWD R1
     * BZFA: same structure, BZFA selects R1 if zero flag, else R2
     */
    imem[p++]=I_LWD_R0; imem[p++]=I_LWD_R1; imem[p++]=I_2OP(OP_LNOR);  imem[p++]=I_SWD_R2;
    imem[p++]=I_LWD_R0; imem[p++]=I_LWD_R1; imem[p++]=I_2OP(OP_LNXOR); imem[p++]=I_SWD_R2;
    /* BSFA */
    imem[p++]=I_LWD_R0; imem[p++]=I_LWD_R1; imem[p++]=I_LWD_R2;
    imem[p++]=INSTR(SRC_R0, SRC_ZERO, OP_SADD, 0, 1, 0, 0); /* flag from cond */
    imem[p++]=INSTR(SRC_R1, SRC_R2,   OP_BSFA, 1, 1, 0, 0); /* sel on sign flag */
    imem[p++]=I_SWD_R1;
    /* BZFA */
    imem[p++]=I_LWD_R0; imem[p++]=I_LWD_R1; imem[p++]=I_LWD_R2;
    imem[p++]=INSTR(SRC_R0, SRC_ZERO, OP_SADD, 0, 1, 0, 0); /* flag from cond */
    imem[p++]=INSTR(SRC_R1, SRC_R2,   OP_BZFA, 1, 1, 0, 0); /* sel on zero flag */
    imem[p++]=I_SWD_R1;
    imem[p++]=I_EXIT;    /* p == 76 */
    kmem[3]   = KW1(55, 21);

    /* ── K4: BEQ BNE BLT BGE not-taken (start=76, n=17) ────────────────
     * Not-taken branches: cmp_result=0, br_req=0, alu_res_o=0 → SWD writes 0
     */
    imem[p++]=I_LWD_R0; imem[p++]=I_LWD_R1; imem[p++]=I_2OP(OP_BEQ); imem[p++]=I_SWD_R2;
    imem[p++]=I_LWD_R0; imem[p++]=I_LWD_R1; imem[p++]=I_2OP(OP_BNE); imem[p++]=I_SWD_R2;
    imem[p++]=I_LWD_R0; imem[p++]=I_LWD_R1; imem[p++]=I_2OP(OP_BLT); imem[p++]=I_SWD_R2;
    imem[p++]=I_LWD_R0; imem[p++]=I_LWD_R1; imem[p++]=I_2OP(OP_BGE); imem[p++]=I_SWD_R2;
    imem[p++]=I_EXIT;    /* p == 93 */
    kmem[4]   = KW1(76, 17);
}

/* ── Kernel runner ─────────────────────────────────────────────────────── */

static void run_kernel(int kid, int32_t *rptr, int32_t *wptr)
{
    cgra_set_read_ptr (&cgra, (uint32_t)rptr, 0);
    cgra_set_write_ptr(&cgra, (uint32_t)wptr, 0);
    cgra_intr_flag = 0;
    cgra_set_kernel(&cgra, kid);
    while (!cgra_intr_flag)
        wait_for_interrupt();
}

/* ── main ──────────────────────────────────────────────────────────────── */

int main(void)
{
    /* Interrupt setup */
    plic_Init();
    plic_irq_set_priority(CGRA_INTR, 1);
    plic_irq_set_enabled(CGRA_INTR, kPlicToggleEnabled);
    plic_assign_external_irq_handler(CGRA_INTR, &handler_irq_cgra);
    CSR_SET_BITS(CSR_REG_MSTATUS, 0x8);
    CSR_SET_BITS(CSR_REG_MIE, 1 << 11);
    cgra_intr_flag = 0;
    cgra.base_addr = mmio_region_from_addr((uintptr_t)CGRA_PERIPH_START_ADDRESS);

    /* ── Test values ────────────────────────────────────────────────── */
    const int32_t A  = 7;
    const int32_t B  = 2;
    const int32_t SH = 1;   /* shift amount */

    /* K5: 1 input */
    input_buf[0] = A;
    /* K1: 9 inputs */
    input_buf[1]=A; input_buf[2]=B;   /* SADD */
    input_buf[3]=A; input_buf[4]=B;   /* SSUB */
    input_buf[5]=A; input_buf[6]=B;   /* SMUL */
    input_buf[7]=A; input_buf[8]=B;   /* FXPMUL */
    input_buf[9]=A;                   /* SABS */
    /* K2: 14 inputs */
    input_buf[10]=A; input_buf[11]=SH; /* SLL */
    input_buf[12]=A; input_buf[13]=SH; /* SRL */
    input_buf[14]=A; input_buf[15]=SH; /* SRA */
    input_buf[16]=A; input_buf[17]=B;  /* LAND */
    input_buf[18]=A; input_buf[19]=B;  /* LOR  */
    input_buf[20]=A; input_buf[21]=B;  /* LXOR */
    input_buf[22]=A; input_buf[23]=B;  /* LNAND*/
    /* K3: 10 inputs */
    input_buf[24]=A;  input_buf[25]=B;  /* LNOR  */
    input_buf[26]=A;  input_buf[27]=B;  /* LNXOR */
    input_buf[28]=-5; input_buf[29]=1; input_buf[30]=10; /* BSFA: cond<0 → sign → tval */
    input_buf[31]=0;  input_buf[32]=1; input_buf[33]=10; /* BZFA: cond=0 → zero → tval */
    /* K4: 8 inputs */
    input_buf[34]=7; input_buf[35]=2; /* BEQ:  7≠2 → not taken */
    input_buf[36]=5; input_buf[37]=5; /* BNE:  5==5 → not taken */
    input_buf[38]=5; input_buf[39]=5; /* BLT:  5≥5 → not taken */
    input_buf[40]=1; input_buf[41]=5; /* BGE:  1<5 → not taken */

    /* ── Expected results ───────────────────────────────────────────── */
    const int32_t expected[21] = {
        /* [0]  JUMP  */ A,
        /* [1]  SADD  */ A + B,
        /* [2]  SSUB  */ A - B,
        /* [3]  SMUL  */ A * B,
        /* [4]  FXPMUL*/ fxp_ref(A, B),
        /* [5]  SABS  */ A,           /* A=7 > 0 */
        /* [6]  SLL   */ (int32_t)((uint32_t)A << SH),
        /* [7]  SRL   */ (int32_t)((uint32_t)A >> SH),
        /* [8]  SRA   */ A >> SH,
        /* [9]  LAND  */ A & B,
        /* [10] LOR   */ A | B,
        /* [11] LXOR  */ A ^ B,
        /* [12] LNAND */ ~(A & B),
        /* [13] LNOR  */ ~(A | B),
        /* [14] LNXOR */ ~(A ^ B),
        /* [15] BSFA  */ 1,           /* cond=-5 → sign flag → tval=1 */
        /* [16] BZFA  */ 1,           /* cond=0  → zero flag → tval=1 */
        /* [17] BEQ   */ 0,           /* not taken */
        /* [18] BNE   */ 0,           /* not taken */
        /* [19] BLT   */ 0,           /* not taken */
        /* [20] BGE   */ 0,           /* not taken */
    };

    static const char *names[21] = {
        "JUMP","SADD","SSUB","SMUL","FXPMUL","SABS",
        "SLL","SRL","SRA",
        "LAND","LOR","LXOR","LNAND","LNOR","LNXOR",
        "BSFA","BZFA",
        "BEQ","BNE","BLT","BGE"
    };

    /* ── Build and load bitstream ───────────────────────────────────── */
    build_bitstream();
    cgra_cmem_init(imem, kmem);

    /* ── Run kernels ────────────────────────────────────────────────── */
    run_kernel(5, &input_buf[0],  &output_buf[0]);  /* K5: JUMP  → out[0]    */
    run_kernel(1, &input_buf[1],  &output_buf[1]);  /* K1: arith → out[1..5] */
    run_kernel(2, &input_buf[10], &output_buf[6]);  /* K2: shift → out[6..12]*/
    run_kernel(3, &input_buf[24], &output_buf[13]); /* K3: flags → out[13..16]*/
    run_kernel(4, &input_buf[34], &output_buf[17]); /* K4: branch→ out[17..20]*/

    /* ── Check results ──────────────────────────────────────────────── */
    PRINTF("=== CGRA ALU test ===\n");
    int total_errors = 0;
    for (int i = 0; i < 21; i++) {
        int ok = (output_buf[i] == expected[i]);
        if (!ok) {
            PRINTF("  %-6s got 0x%08X (%d), expected 0x%08X (%d)\n",
                   names[i],
                   (unsigned)output_buf[i], (int)output_buf[i],
                   (unsigned)expected[i],   (int)expected[i]);
            total_errors++;
        }
        PRINTF("%-6s %s\n", names[i], ok ? "PASS" : "FAIL");
    }
    PRINTF("\nfinished with %d errors\n", total_errors);
    PRINTF("### DONE ###\n");
    return total_errors;
}
