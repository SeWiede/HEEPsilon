/*
 * cgra_loop_preempt — single-address async CGRA preemption via LWI
 *
 * The kernel polls a preempt flag at a FIXED address every iteration using
 * the LWI (load word indirect) instruction. LWI takes the address from a
 * register (muxB) and bypasses the sequential read pointer entirely — the
 * pointer never advances on LWI calls.
 *
 * This means the CPU can stop the CGRA with a SINGLE STORE to one fixed
 * address at any time, without knowing the kernel's current iteration:
 *
 *     preempt_flag = 1;   // that's it
 *
 * Kernel startup loads &preempt_flag into R1 via one LWD (sequential,
 * consumes one input slot). Every loop iteration then uses LWI R0,[R1]
 * to read the flag without advancing the pointer.
 *
 * Sweep: CPU busy-loops for DELAY iterations before writing the flag.
 * Different delays → different CGRA iteration counts at preemption.
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

#ifdef DEBUG
  #define PRINTF(fmt, ...) printf(fmt, ##__VA_ARGS__)
#else
  #define PRINTF(...)
#endif

/* ── Instruction encoding ─────────────────────────────────────────────────── */
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

#define OP_SADD  1
#define OP_BNE   17
#define OP_JUMP  20
#define OP_LWD   21
#define OP_SWD   22
#define OP_LWI   23   /* load from address in muxB register — no pointer advance */
#define OP_EXIT  25

#define KMEM_WORD(cols, start, n) \
    (((uint32_t)(cols) << 12) | ((uint32_t)(start) << 5) | ((uint32_t)(n) - 1))

/* ── Parameters ───────────────────────────────────────────────────────────── */
#define MAX_ITER 5000

static const int DELAYS[] = {0, 50, 150, 300, 500};
#define N_SWEEP ((int)(sizeof(DELAYS) / sizeof(DELAYS[0])))

/* ── Globals ──────────────────────────────────────────────────────────────── */
static volatile int cgra_done;
static cgra_t       cgra;

/* THE preemption address — CPU writes 1 here to stop the CGRA */
static volatile int32_t preempt_flag;

/* Input: just one word — the address of preempt_flag */
static int32_t input_buf[1] __attribute__((aligned(4)));

/* Output: running iteration counter written by the CGRA */
static volatile int32_t output_buf[MAX_ITER] __attribute__((aligned(4)));

static uint32_t imem[CGRA_CMEM_TOT_DEPTH];
static uint32_t kmem[CGRA_KMEM_DEPTH];

void handler_irq_cgra(uint32_t id) { cgra_done = 1; }

/* ── Bitstream ────────────────────────────────────────────────────────────── */
static void build_bitstream(void)
{
    memset(imem, 0, sizeof(imem));
    memset(kmem, 0, sizeof(kmem));

    /*
     * Kernel 1 — 7 instructions, bank 0 (row 0)
     *
     *   PC 0: LWD R1           load &preempt_flag from input (sequential, once)
     *   PC 1: LWI R0, [R1]     R0 = *R1 = preempt_flag  (NO pointer advance)
     *   PC 2: BNE(R0,0) → 6   exit if preempt_flag ≠ 0
     *   PC 3: SADD(R2,1)→R2   R2++ (iteration counter)
     *   PC 4: SWD R2           write count to output_buf (sequential)
     *   PC 5: JUMP 1           loop back to LWI
     *   PC 6: EXIT
     *
     * The key: LWI reads from the address held in R1 without consuming any
     * sequential slot — same physical address re-read every iteration.
     * CPU stops the CGRA with a single write: preempt_flag = 1.
     */
    imem[0] = INSTR(0,        0,        OP_LWD,  1, 1, 0, 4); /* LWD R1 */
    imem[1] = INSTR(0,        SRC_R1,   OP_LWI,  0, 1, 0, 0); /* LWI R0,[R1] */
    imem[2] = INSTR(SRC_R0,   SRC_ZERO, OP_BNE,  0, 0, 0, 6); /* BNE→6 */
    imem[3] = INSTR(SRC_R2,   SRC_IMM,  OP_SADD, 2, 1, 0, 1); /* R2=R2+1 */
    imem[4] = INSTR(SRC_R2,   0,        OP_SWD,  0, 0, 0, 4); /* SWD R2 */
    imem[5] = INSTR(SRC_ZERO, SRC_IMM,  OP_JUMP, 0, 0, 0, 1); /* JUMP 1 */
    imem[6] = INSTR(0,        0,        OP_EXIT, 0, 0, 0, 0); /* EXIT */
    kmem[1] = KMEM_WORD(0x1, 0, 7);
}

/* ── main ─────────────────────────────────────────────────────────────────── */
int main(void)
{
    plic_Init();
    plic_irq_set_priority(CGRA_INTR, 1);
    plic_irq_set_enabled(CGRA_INTR, kPlicToggleEnabled);
    plic_assign_external_irq_handler(CGRA_INTR, &handler_irq_cgra);
    CSR_SET_BITS(CSR_REG_MSTATUS, 0x8);
    CSR_SET_BITS(CSR_REG_MIE, 1 << 11);
    cgra.base_addr = mmio_region_from_addr((uintptr_t)CGRA_PERIPH_START_ADDRESS);

    build_bitstream();
    cgra_cmem_init(imem, kmem);

    /* Input is always the same: the address of preempt_flag */
    input_buf[0] = (int32_t)&preempt_flag;

    int total_errors = 0;

    for (int s = 0; s < N_SWEEP; s++) {
        int delay = DELAYS[s];

        preempt_flag = 0;
        for (int i = 0; i < MAX_ITER; i++) output_buf[i] = 0;

        cgra_wait_ready(&cgra);
        cgra_set_read_ptr (&cgra, (uint32_t)input_buf,  0);
        cgra_set_write_ptr(&cgra, (uint32_t)output_buf, 0);
        cgra_done = 0;
        cgra_set_kernel(&cgra, 1);

        /* CPU works for DELAY iterations — CGRA runs freely in parallel */
        volatile int cpu_work = 0;
        while (cpu_work < delay) cpu_work++;

        /* Preempt: one store to one fixed address — no slot tracking */
        preempt_flag = 1;

        volatile int cpu_post = 0;
        while (!cgra_done) cpu_post++;

        /* Count completed iterations */
        int cgra_iters = 0;
        while (cgra_iters < MAX_ITER && output_buf[cgra_iters] != 0)
            cgra_iters++;

        printf("cpu_delay=%-4d  cgra_iters=%d\n", delay, cgra_iters);

        /* Verify counter values: output_buf[i] must equal i+1 */
        int errors = 0;
        for (int i = 0; i < cgra_iters; i++) {
            if (output_buf[i] != i + 1) {
                PRINTF("  [%d] got %d expected %d FAIL\n", i, (int)output_buf[i], i+1);
                errors++;
            }
        }
        for (int i = cgra_iters; i < MAX_ITER; i++) {
            if (output_buf[i] != 0) {
                PRINTF("  [%d] got %d expected 0 FAIL\n", i, (int)output_buf[i]);
                errors++;
            }
        }
        PRINTF("  -> %d errors\n", errors);
        total_errors += errors;
    }

    printf("Total errors: %d\n", total_errors);
    return total_errors;
}
