/*
 * cgra_cpu_parallel — demonstrates CPU/CGRA parallel execution
 *
 * Launches a SADD/SSUB/SMUL/FXPMUL/SABS kernel on the CGRA, then counts
 * in a busy loop on the CPU until the CGRA interrupt fires. Prints the
 * count so you can observe how many CPU iterations ran during CGRA execution.
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

#define OP_SADD   1
#define OP_SSUB   2
#define OP_SMUL   3
#define OP_FXPMUL 4
#define OP_SWD    22
#define OP_LWD    21
#define OP_EXIT   25
#define OP_SABS   26

/* KMEM word: [15:12]=col_mask [11:5]=start_addr [4:0]=(n_instr-1) */
#define KMEM_WORD(cols, start, n) \
    (((uint32_t)(cols) << 12) | ((uint32_t)(start) << 5) | ((uint32_t)(n) - 1))

/* ── Globals ──────────────────────────────────────────────────────────────── */

static volatile int cgra_done;
static cgra_t       cgra;

/* 9 inputs, 5 outputs — col 0 only */
static int32_t input_buf [9] __attribute__((aligned(4)));
static int32_t output_buf[5] __attribute__((aligned(4)));

static uint32_t imem[CGRA_CMEM_TOT_DEPTH];
static uint32_t kmem[CGRA_KMEM_DEPTH];

void handler_irq_cgra(uint32_t id) { cgra_done = 1; }

static int32_t fxp_ref(int32_t a, int32_t b) {
    return (int32_t)(((int64_t)a * b) >> 15);
}

/* ── Bitstream ────────────────────────────────────────────────────────────── */

static void build_bitstream(void)
{
    memset(imem, 0, sizeof(imem));
    memset(kmem, 0, sizeof(kmem));

    /*
     * K1 (col 0, start=0, n=20):
     *   LWD R0, LWD R1, SADD(R0,R1)→R2,   SWD R2   → out[0]
     *   LWD R0, LWD R1, SSUB(R0,R1)→R2,   SWD R2   → out[1]
     *   LWD R0, LWD R1, SMUL(R0,R1)→R2,   SWD R2   → out[2]
     *   LWD R0, LWD R1, FXPMUL(R0,R1)→R2, SWD R2   → out[3]
     *   LWD R0,         SABS(R0)→R1,       SWD R1   → out[4]
     *   EXIT
     */
#define I_LWD_R0  INSTR(0, 0, OP_LWD, 0, 1, 0, 4)
#define I_LWD_R1  INSTR(0, 0, OP_LWD, 1, 1, 0, 4)
#define I_SWD_R1  INSTR(SRC_R1, 0, OP_SWD, 0, 0, 0, 4)
#define I_SWD_R2  INSTR(SRC_R2, 0, OP_SWD, 0, 0, 0, 4)
#define I_2OP(op) INSTR(SRC_R0, SRC_R1, (op), 2, 1, 0, 0)

    int p = 0;
    imem[p++]=I_LWD_R0; imem[p++]=I_LWD_R1; imem[p++]=I_2OP(OP_SADD);   imem[p++]=I_SWD_R2;
    imem[p++]=I_LWD_R0; imem[p++]=I_LWD_R1; imem[p++]=I_2OP(OP_SSUB);   imem[p++]=I_SWD_R2;
    imem[p++]=I_LWD_R0; imem[p++]=I_LWD_R1; imem[p++]=I_2OP(OP_SMUL);   imem[p++]=I_SWD_R2;
    imem[p++]=I_LWD_R0; imem[p++]=I_LWD_R1; imem[p++]=I_2OP(OP_FXPMUL); imem[p++]=I_SWD_R2;
    imem[p++]=I_LWD_R0;
    imem[p++]=INSTR(SRC_R0, SRC_ZERO, OP_SABS, 1, 1, 0, 0);
    imem[p++]=I_SWD_R1;
    imem[p++]=INSTR(0, 0, OP_EXIT, 0, 0, 0, 0); /* p == 20 */

    kmem[1] = KMEM_WORD(0x1, 0, 20);
}

/* ── main ─────────────────────────────────────────────────────────────────── */

int main(void)
{
    /* Interrupt setup */
    plic_Init();
    plic_irq_set_priority(CGRA_INTR, 1);
    plic_irq_set_enabled(CGRA_INTR, kPlicToggleEnabled);
    plic_assign_external_irq_handler(CGRA_INTR, &handler_irq_cgra);
    CSR_SET_BITS(CSR_REG_MSTATUS, 0x8);
    CSR_SET_BITS(CSR_REG_MIE, 1 << 11);
    cgra_done = 0;
    cgra.base_addr = mmio_region_from_addr((uintptr_t)CGRA_PERIPH_START_ADDRESS);

    /* Input values */
    const int32_t A = 12;
    const int32_t B = -5;
    input_buf[0]=A; input_buf[1]=B;  /* SADD  */
    input_buf[2]=A; input_buf[3]=B;  /* SSUB  */
    input_buf[4]=A; input_buf[5]=B;  /* SMUL  */
    input_buf[6]=A; input_buf[7]=B;  /* FXPMUL*/
    input_buf[8]=B;                  /* SABS: |B|  */

    const int32_t expected[5] = {
        A + B,
        A - B,
        A * B,
        fxp_ref(A, B),
        (B < 0 ? -B : B),
    };

    PRINTF("Building and loading CGRA bitstream...\n");
    build_bitstream();
    cgra_cmem_init(imem, kmem);

    /* Set pointers and launch */
    cgra_set_read_ptr (&cgra, (uint32_t)input_buf,  0);
    cgra_set_write_ptr(&cgra, (uint32_t)output_buf, 0);
    cgra_done = 0;
    cgra_set_kernel(&cgra, 1);

    /* CPU counts while CGRA executes */
    volatile int count = 0;
    while (!cgra_done) {
        count++;
    }

    printf("CPU counted %d iterations while CGRA ran\n", count);

    /* Verify CGRA results */
    PRINTF("Checking results...\n");
    int errors = 0;
    const char *names[5] = { "SADD", "SSUB", "SMUL", "FXPMUL", "SABS" };
    for (int i = 0; i < 5; i++) {
        int ok = (output_buf[i] == expected[i]);
        PRINTF("  %-6s got %d, expected %d %s\n",
               names[i], output_buf[i], expected[i], ok ? "OK" : "FAIL");
        if (!ok) errors++;
    }

    PRINTF("Finished with %d errors\n", errors);
    PRINTF("### DONE ###\n");
    return errors;
}
