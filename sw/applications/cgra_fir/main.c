/*
 * cgra_fir — 4-tap FIR filter on the CGRA
 *
 * Filter:  y[n] = h[0]*x[n] + h[1]*x[n-1] + h[2]*x[n-2] + h[3]*x[n-3]
 * Coefficients: h = {1, 2, 2, 1}  (symmetric low-pass FIR)
 *
 * CGRA mapping
 * ============
 *   - Only ROW 0 is programmed; rows 1-3 run NOP (all-zero CMEM).
 *   - All 4 COLUMNS are active.  Column k computes one output sample y[n+k]
 *     from x[n+k-3..n+k], so 4 output samples are produced per kernel call.
 *   - Each column has its own read/write pointer (set via cgra_set_read_ptr /
 *     cgra_set_write_ptr before every call).
 *
 * Instruction sequence (row 0, 15 instructions at CMEM addresses 0-14):
 *
 *   PC  0  0x000B0000   SADD(0, 0)      → reg[1]       init acc = 0
 *   PC  1  0x00A90004   LWD  stride=4   → reg[0]       load x[n+k-3]
 *   PC  2  0x6A180001   SMUL(reg[0], 1)                 ×h[3]=1, stalls 3 cyc
 *   PC  3  0x170B0000   SADD(own_res, reg[1]) → reg[1]  acc += x[n+k-3]*1
 *   PC  4  0x00A90004   LWD  stride=4   → reg[0]       load x[n+k-2]
 *   PC  5  0x6A180002   SMUL(reg[0], 2)                 ×h[2]=2, stalls
 *   PC  6  0x170B0000   SADD(own_res, reg[1]) → reg[1]  acc += x[n+k-2]*2
 *   PC  7  0x00A90004   LWD  stride=4   → reg[0]       load x[n+k-1]
 *   PC  8  0x6A180002   SMUL(reg[0], 2)                 ×h[1]=2, stalls
 *   PC  9  0x170B0000   SADD(own_res, reg[1]) → reg[1]  acc += x[n+k-1]*2
 *   PC 10  0x00A90004   LWD  stride=4   → reg[0]       load x[n+k]
 *   PC 11  0x6A180001   SMUL(reg[0], 1)                 ×h[0]=1, stalls
 *   PC 12  0x170B0000   SADD(own_res, reg[1]) → reg[1]  acc = y[n+k]
 *   PC 13  0x70B00010   SWD(reg[1], stride=16)          write y[n+k]
 *   PC 14  0x00C80000   EXIT
 *
 * Instruction encoding reference
 * ===============================
 *   bits[31:28] mux_a:  0=zero 1=own_res 2=left 3=right 4=top 5=bot
 *                        6=reg[0] 7=reg[1] 8=reg[2] 9=reg[3] 10=imm
 *   bits[27:24] mux_b:  same encoding
 *   bits[23:19] opcode: 1=SADD 3=SMUL 21=LWD 22=SWD 25=EXIT
 *   bits[18:17] reg_sel (destination register index)
 *   bits[16]    reg_we  (write to register file)
 *   bits[12:0]  imm_val (signed, used as LWD/SWD stride in bytes or SMUL coeff)
 *
 * KMEM word format
 * ================
 *   bits[15:12] one-hot column mask
 *   bits[11:5]  CMEM bank start address
 *   bits[4:0]   num_instructions - 1
 */

#include <stdio.h>
#include <stdlib.h>
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

/* ------------------------------------------------------------------ */
/*  Build-time sanity check                                             */
/* ------------------------------------------------------------------ */
#if CGRA_N_COLS != 4 || CGRA_N_ROWS != 4
  #error "cgra_fir requires a 4×4 CGRA"
#endif

/* ------------------------------------------------------------------ */
/*  Debug switch                                                        */
/* ------------------------------------------------------------------ */
#define DEBUG
#ifdef DEBUG
  #define PRINTF(fmt, ...)  printf(fmt, ##__VA_ARGS__)
#else
  #define PRINTF(...)
#endif

/* ------------------------------------------------------------------ */
/*  Filter parameters                                                   */
/* ------------------------------------------------------------------ */
#define N_TAPS      4               /* number of filter taps            */
#define N_OUT      16               /* output samples per test run      */
/* Input array must have N_TAPS-1 prefix zeros + N_OUT samples        */
#define N_IN_PADDED (N_OUT + N_TAPS - 1)   /* = 19                     */

/* FIR coefficients: h[0]=newest tap, h[N_TAPS-1]=oldest tap          */
static const int32_t h[N_TAPS] = {1, 2, 2, 1};

/* ------------------------------------------------------------------ */
/*  CGRA bitstream                                                      */
/* ------------------------------------------------------------------ */
#define FIR_KER_ID  1
#define N_INSTR     15              /* PC 0..14                         */

/*
 * KMEM[1]: one-hot=0xF (all 4 cols), start_addr=0, num_instr-1=14=0xE
 *   = (0xF << 12) | (0 << 5) | 0xE = 0xF00E
 */
static uint32_t cgra_kmem[CGRA_KMEM_DEPTH] = {
    0x0, 0xF00E,  /* kernel 1 */
};

/*
 * CMEM: flat array, row i → indices [i*128 .. i*128+127].
 * Row 0 carries the FIR instructions (indices 0-14).
 * Rows 1-3 are all-zero (NOP), so the EXIT in row 0 terminates all columns.
 */
/*
 * BUG: SADD(own_res, ...) after SMUL does not accumulate correctly.
 *
 * Observed behaviour
 * ------------------
 * Simulation (Verilator) with an impulse input gives:
 *
 *   SW reference: 1 2 2 1 0 0 …
 *   CGRA output:  1 0 0 0 …
 *
 * Column 0 produces the correct result (1); columns 1-3 produce 0.
 *
 * Root cause
 * ----------
 * The intent at PC 3/6/9/12 is:
 *
 *   SADD(own_res_i, reg[1]) → reg[1]     // acc += SMUL_result
 *
 * where own_res_i is expected to hold the SMUL result from the immediately
 * preceding instruction.  own_res_i maps to rcs_res_reg, which (per the RTL
 * in cgra_rcs.sv) is updated only when pc_e_i = 1.  Theory says pc_e goes
 * high on the cycle that the SMUL stall counter (dp_stall_reg in
 * cgra_controller.sv) reaches 0, at the same edge where the PC advances —
 * so own_res_i at the SADD instruction should equal the SMUL result.
 *
 * In practice this does not hold.  The most likely explanation is a
 * one-cycle skew: rcs_res_reg is registered, so its value at the SADD
 * instruction is whatever was present *before* the stall released, not the
 * SMUL result that was written at the stall-release edge.  If that is the
 * case, own_res_i at the SADD equals the result of the *previous* pc_e=1
 * cycle, which is the SADD itself (value 0 initially) rather than the SMUL.
 *
 * Column 0 masks this bug because its non-zero tap (x_padded[3]=1) lands
 * on the very LAST SMUL/SADD pair (PC 11-12).  At that point reg[1] = 0
 * from all the previous (all-zero) accumulations, so whether own_res is
 * the SMUL result (1) or something else that happens to be 1 (the LWD
 * result stored in rcs_res_reg before the SMUL) makes no observable
 * difference.  Columns 1-3 have their non-zero tap at an earlier pair
 * (PC 8-9 for col 1, etc.) and the incorrect own_res produces 0.
 *
 * Fix
 * ---
 * Avoid relying on own_res_i for the SMUL result.  Instead, save the SMUL
 * result directly into reg[2] (reg_we=1, reg_sel=2) and replace the
 * SADD(own_res, reg[1]) with SADD(reg[2], reg[1]).
 *
 * Encoding changes (4 SMUL instructions and 4 SADD instructions):
 *
 *   SMUL without save:  0x6A18xxxx  → bits[18:16] = 000  (reg_we=0)
 *   SMUL with reg[2]:   0x6A1Axxxx  → bits[18:16] = 101  (reg_sel=2, reg_we=1)
 *
 *   SADD(own_res, reg[1]):  0x170B0000  → mux_a bits[31:28] = 0001 = own_res(1)
 *   SADD(reg[2],  reg[1]):  0x870B0000  → mux_a bits[31:28] = 1000 = reg[2](8)
 *
 * Corrected CMEM (only the changed words shown):
 *
 *   PC  2  0x6A1A0001  SMUL(reg[0], 1) → reg[2]    save h[3]*x to reg[2]
 *   PC  3  0x870B0000  SADD(reg[2], reg[1]) → reg[1]
 *   PC  5  0x6A1A0002  SMUL(reg[0], 2) → reg[2]
 *   PC  6  0x870B0000  SADD(reg[2], reg[1]) → reg[1]
 *   PC  8  0x6A1A0002  SMUL(reg[0], 2) → reg[2]
 *   PC  9  0x870B0000  SADD(reg[2], reg[1]) → reg[1]
 *   PC 11  0x6A1A0001  SMUL(reg[0], 1) → reg[2]
 *   PC 12  0x870B0000  SADD(reg[2], reg[1]) → reg[1]
 */
static uint32_t cgra_cmem[CGRA_CMEM_TOT_DEPTH] = {
    /* ---- Row 0 -------------------------------------------------------- */
    /* PC  0 */ 0x000B0000,  /* SADD(zero, zero) → reg[1]   init acc=0   */
    /* PC  1 */ 0x00A90004,  /* LWD stride=4    → reg[0]   load x[n+k-3] */
    /* PC  2 */ 0x6A180001,  /* SMUL(reg[0], 1)             h[3]=1, stall  [BUG: no reg save] */
    /* PC  3 */ 0x170B0000,  /* SADD(own_res, reg[1])→reg[1]               [BUG: own_res wrong] */
    /* PC  4 */ 0x00A90004,  /* LWD stride=4    → reg[0]   load x[n+k-2] */
    /* PC  5 */ 0x6A180002,  /* SMUL(reg[0], 2)             h[2]=2, stall  [BUG] */
    /* PC  6 */ 0x170B0000,  /* SADD(own_res, reg[1])→reg[1]               [BUG] */
    /* PC  7 */ 0x00A90004,  /* LWD stride=4    → reg[0]   load x[n+k-1] */
    /* PC  8 */ 0x6A180002,  /* SMUL(reg[0], 2)             h[1]=2, stall  [BUG] */
    /* PC  9 */ 0x170B0000,  /* SADD(own_res, reg[1])→reg[1]               [BUG] */
    /* PC 10 */ 0x00A90004,  /* LWD stride=4    → reg[0]   load x[n+k]   */
    /* PC 11 */ 0x6A180001,  /* SMUL(reg[0], 1)             h[0]=1, stall  [BUG] */
    /* PC 12 */ 0x170B0000,  /* SADD(own_res, reg[1])→reg[1] acc=y[n+k]   [BUG] */
    /* PC 13 */ 0x70B00010,  /* SWD(reg[1], stride=16)  write y[n+k]      */
    /* PC 14 */ 0x00C80000,  /* EXIT                                       */
    /* PC 15-127: implicit zero (NOP) */
    /* ---- Rows 1-3: all-zero (NOP) ---- */
};

/* ------------------------------------------------------------------ */
/*  Interrupt handler                                                   */
/* ------------------------------------------------------------------ */
static volatile int8_t cgra_intr_flag;

void handler_irq_cgra(uint32_t id) {
    cgra_intr_flag = 1;
}

/* ------------------------------------------------------------------ */
/*  Software reference FIR                                              */
/* ------------------------------------------------------------------ */
static void fir_sw(const int32_t *x_padded, int32_t *y, int n_out)
{
    /* x_padded has (N_TAPS-1) leading zeros followed by n_out samples.
     * y[n] = sum_k h[k] * x[n-k]  with h[0]=newest tap.              */
    for (int n = 0; n < n_out; n++) {
        int32_t acc = 0;
        for (int k = 0; k < N_TAPS; k++) {
            /* x_padded[n + (N_TAPS-1) - k] = x[n-k]                  */
            acc += h[k] * x_padded[n + (N_TAPS - 1) - k];
        }
        y[n] = acc;
    }
}

/* ------------------------------------------------------------------ */
/*  main                                                                */
/* ------------------------------------------------------------------ */
int main(void)
{
    /* ---- Test signal: impulse (verifies each coefficient separately) */
    int32_t x_padded[N_IN_PADDED] __attribute__((aligned(4))) = {0};
    /* first N_TAPS-1 entries stay 0 (boundary padding)                */
    x_padded[N_TAPS - 1] = 1;  /* x[0] = 1, rest = 0 (impulse)        */

    /* ---- SW reference ------------------------------------------------ */
    int32_t y_sw[N_OUT];
    fir_sw(x_padded, y_sw, N_OUT);

    PRINTF("SW reference: ");
    for (int i = 0; i < N_OUT; i++) PRINTF("%d ", y_sw[i]);
    PRINTF("\n");

    /* ---- Load CGRA bitstream ----------------------------------------- */
    PRINTF("Init CGRA context memory...");
    cgra_cmem_init(cgra_cmem, cgra_kmem);
    PRINTF("done\n");

    /* ---- Interrupt setup -------------------------------------------- */
    plic_Init();
    plic_irq_set_priority(CGRA_INTR, 1);
    plic_irq_set_enabled(CGRA_INTR, kPlicToggleEnabled);
    plic_assign_external_irq_handler(CGRA_INTR, &handler_irq_cgra);

    CSR_SET_BITS(CSR_REG_MSTATUS, 0x8);
    const uint32_t mask = 1 << 11;
    CSR_SET_BITS(CSR_REG_MIE, mask);

    /* ---- CGRA handle -------------------------------------------------- */
    cgra_t cgra;
    cgra.base_addr = mmio_region_from_addr((uintptr_t)CGRA_PERIPH_START_ADDRESS);
    cgra_perf_cnt_enable(&cgra, 1);

    /* ---- Output array ------------------------------------------------- */
    int32_t y_cgra[N_OUT] __attribute__((aligned(4))) = {0};

    /*
     * Process N_OUT samples in batches of 4.
     * For batch j (j = 0..N_BATCHES-1):
     *   col k (k=0..3) computes y[4j+k] = sum_t h[t] * x_padded[4j+k + t]
     *                                    (h ordered oldest→newest)
     *
     * Read pointers: col k → &x_padded[4j + k]
     *   (x_padded[4j+k .. 4j+k+3] provides the 4 taps)
     * Write pointers: col k → &y_cgra[4j + k]
     *   After SWD with stride=16 bytes, ptr advances 4 int32s to next batch.
     *
     * Because the pointers advance automatically by the hardware (LWD/SWD
     * stride), we set them once per batch here to be explicit and safe.
     */
#define N_BATCHES (N_OUT / CGRA_N_COLS)

    for (int batch = 0; batch < N_BATCHES; batch++) {
        int base = batch * CGRA_N_COLS;  /* first output index in batch  */

        cgra_wait_ready(&cgra);

        /* x_padded[base + k] is the OLDEST tap needed for y[base+k].   */
        for (int col = 0; col < CGRA_N_COLS; col++) {
            cgra_set_read_ptr (&cgra, (uint32_t)&x_padded[base + col], col);
            cgra_set_write_ptr(&cgra, (uint32_t)&y_cgra [base + col], col);
        }

        cgra_intr_flag = 0;
        cgra_set_kernel(&cgra, FIR_KER_ID);

        while (cgra_intr_flag == 0) {
            wait_for_interrupt();
        }

        PRINTF("batch %d: y[%d..%d] = ", batch, base, base + 3);
        for (int col = 0; col < CGRA_N_COLS; col++) {
            PRINTF("%d ", y_cgra[base + col]);
        }
        PRINTF("\n");
    }

    /* ---- Compare ------------------------------------------------------ */
    int32_t errors = 0;
    for (int i = 0; i < N_OUT; i++) {
        if (y_cgra[i] != y_sw[i]) {
            printf("[%d] CGRA=%d  SW=%d\n", i, y_cgra[i], y_sw[i]);
            errors++;
        }
    }

    printf("CGRA FIR finished with %d errors\n", errors);
    printf("### DONE ###\n");
    return errors ? EXIT_FAILURE : EXIT_SUCCESS;
}
