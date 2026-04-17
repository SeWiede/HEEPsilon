/*
 * SABS kernel — Signed ABSolute value
 *
 * Demonstrates the new CGRA_ALU_SABS operation (opcode 5'b11010 = 26).
 * This is a hardware/software co-design example: the ALU op was added to
 * cgra_pkg.sv and alu.sv, then exercised here.
 *
 * Kernel (row 0, all 4 columns, 4 instructions):
 *
 *   Opcode encoding:
 *     bits[31:28] mux_a   bits[27:24] mux_b
 *     bits[23:19] opcode  bits[18:17] reg_sel  bits[16] reg_we
 *     bits[12:0]  imm_val (LWD/SWD: stride in bytes)
 *
 *   PC 0  0x00A90000  LWD  stride=0 → reg[0]     load x from read_ptr
 *   PC 1  0x60D30000  SABS(reg[0]) → reg[1]      |x|, new ALU op
 *   PC 2  0x70B00000  SWD(reg[1], stride=0)       write |x| to write_ptr
 *   PC 3  0x00C80000  EXIT
 *
 *   Instruction encoding details:
 *     PC 1: mux_a=6(reg[0]) mux_b=0(zero) op=26(SABS=11010) reg_sel=1 reg_we=1
 *           bits[31:24]=0x60  bits[23:16]=0xD3  bits[15:0]=0x0000
 *
 *   KMEM[1] = 0xF003: one-hot=0xF (all 4 cols), start=0, num_instr-1=3
 *
 * Note on INT_MIN: abs(INT32_MIN) overflows signed 32-bit — both the hardware
 * (~a+1 = INT_MIN) and the software reference return INT_MIN for this input.
 * The check therefore passes on that edge case even though the mathematical
 * result is not representable.
 */

#define _SABS_C

#include <stdint.h>
#include "sabs.h"

#if CGRA_N_COLS == 4

#define CGRA_COLS    4
#define IN_VAR_DEPTH  1
#define OUT_VAR_DEPTH 1

static void     config  (void);
static void     software(void);
static uint32_t check   (void);

/* ------------------------------------------------------------------ */
/*  Bitstream                                                           */
/* ------------------------------------------------------------------ */

static const uint32_t cgra_imem_sabs[CGRA_CMEM_TOT_DEPTH] = {
    /* Row 0 */
    /* PC 0 */ 0x00A90000,  /* LWD  stride=0 → reg[0]  */
    /* PC 1 */ 0x60D30000,  /* SABS(reg[0])  → reg[1]  */
    /* PC 2 */ 0x70B00000,  /* SWD(reg[1],   stride=0) */
    /* PC 3 */ 0x00C80000,  /* EXIT                    */
    /* PC 4-127: zero (NOP) */
    /* Rows 1-3: all-zero (NOP) */
};

static uint32_t cgra_kmem_sabs[CGRA_KMEM_DEPTH] = {
    0x0, 0xF003,  /* kernel 1: all 4 cols, start=0, 4 instr */
};

/* ------------------------------------------------------------------ */
/*  I/O buffers                                                         */
/* ------------------------------------------------------------------ */

static int32_t cgra_input [CGRA_COLS][IN_VAR_DEPTH]  __attribute__((aligned(4)));
static int32_t cgra_output[CGRA_COLS][OUT_VAR_DEPTH] __attribute__((aligned(4)));

/* Software-side copies */
static int32_t i_val[CGRA_COLS];
static int32_t o_sw [CGRA_COLS];
static int32_t o_cgra[CGRA_COLS];

/* ------------------------------------------------------------------ */
/*  Kernel descriptor                                                   */
/* ------------------------------------------------------------------ */

extern kcom_kernel_t sabs_kernel = {
    .kmem   = cgra_kmem_sabs,
    .imem   = (kcom_mem_t)cgra_imem_sabs,
    .col_n  = CGRA_COLS,
    .in_n   = IN_VAR_DEPTH,
    .out_n  = OUT_VAR_DEPTH,
    .input  = (kcom_io_t)cgra_input,
    .output = (kcom_io_t)cgra_output,
    .config = config,
    .func   = software,
    .check  = check,
    .name   = "SABS",
};

/* ------------------------------------------------------------------ */
/*  config: generate random test values (mix of positive and negative) */
/* ------------------------------------------------------------------ */

static void config(void)
{
    for (int c = 0; c < CGRA_COLS; c++) {
        /* Cast to int32_t so ~half of random values are negative */
        i_val[c] = (int32_t)kcom_getRand();
        cgra_input[c][0] = i_val[c];
    }
}

/* ------------------------------------------------------------------ */
/*  software: compute |x| for each column                              */
/* ------------------------------------------------------------------ */

static void software(void)
{
    for (int c = 0; c < CGRA_COLS; c++) {
        o_sw[c] = (i_val[c] < 0) ? -i_val[c] : i_val[c];
    }
}

/* ------------------------------------------------------------------ */
/*  check: compare CGRA output against software reference              */
/* ------------------------------------------------------------------ */

static uint32_t check(void)
{
    uint32_t errors = 0;

    for (int c = 0; c < CGRA_COLS; c++) {
        o_cgra[c] = cgra_output[c][0];

#if PRINT_RESULTS
        PRINTF("col%d: in=%d  cgra=%d  sw=%d%s\n",
               c, i_val[c], o_cgra[c], o_sw[c],
               (o_cgra[c] != o_sw[c]) ? "  WRONG" : "");
#endif

        if (o_cgra[c] != o_sw[c]) {
            errors++;
        }
    }

    return errors;
}

#else
  #error "sabs kernel is only implemented for CGRA_N_COLS == 4"
#endif
