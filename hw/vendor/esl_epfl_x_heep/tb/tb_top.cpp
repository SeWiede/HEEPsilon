// Copyright 2022 OpenHW Group
// Solderpad Hardware License, Version 2.1, see LICENSE.md for details.
// SPDX-License-Identifier: Apache-2.0 WITH SHL-2.1

#include "verilated.h"
#include "verilated_fst_c.h"
#include "Vtestharness.h"
#include "Vtestharness__Syms.h"

#include <stdlib.h>
#include <iostream>
#include <iomanip>

#include "XHEEP_CmdLineOptions.hh"

vluint64_t sim_time = 0;

static void print_core_diag(Vtestharness* dut, const char* phase) {
  auto* root = dut->rootp;
  std::ios::fmtflags prev_flags = std::cout.flags();
  char prev_fill = std::cout.fill();

  std::cout << "[DIAG] " << phase
            << " cycles=" << (sim_time / CLK_PERIOD_ps)
            << " exit_valid=" << static_cast<int>(dut->exit_valid_o)
            << " exit_value=" << dut->exit_value_o
            << " fetch_en=" << static_cast<int>(
                root->testharness__DOT__heepsilon_top_i__DOT__x_heep_system_i__DOT__core_v_mini_mcu_i__DOT__cpu_subsystem_i__DOT__gen_cv32e20__DOT__cv32e20_i__DOT__u_cve2_top__DOT__fetch_enable_q)
            << " instr_req=" << static_cast<int>(
                root->testharness__DOT__heepsilon_top_i__DOT__x_heep_system_i__DOT__core_v_mini_mcu_i__DOT__cpu_subsystem_i__DOT__gen_cv32e20__DOT__cv32e20_i__DOT__u_cve2_top__DOT__u_cve2_core__DOT__instr_req_int)
            << " instr_valid=" << static_cast<int>(
                root->testharness__DOT__heepsilon_top_i__DOT__x_heep_system_i__DOT__core_v_mini_mcu_i__DOT__cpu_subsystem_i__DOT__gen_cv32e20__DOT__cv32e20_i__DOT__u_cve2_top__DOT__u_cve2_core__DOT__if_stage_i__DOT__instr_valid_id_q)
            << " ctrl_busy=" << static_cast<int>(
                root->testharness__DOT__heepsilon_top_i__DOT__x_heep_system_i__DOT__core_v_mini_mcu_i__DOT__cpu_subsystem_i__DOT__gen_cv32e20__DOT__cv32e20_i__DOT__u_cve2_top__DOT__u_cve2_core__DOT__ctrl_busy)
            << " debug_mode=" << static_cast<int>(
                root->testharness__DOT__heepsilon_top_i__DOT__x_heep_system_i__DOT__core_v_mini_mcu_i__DOT__cpu_subsystem_i__DOT__gen_cv32e20__DOT__cv32e20_i__DOT__u_cve2_top__DOT__u_cve2_core__DOT__id_stage_i__DOT__controller_i__DOT__debug_mode_q)
            << " boot_sel_reg=" << static_cast<int>(
                root->testharness__DOT__heepsilon_top_i__DOT__x_heep_system_i__DOT__core_v_mini_mcu_i__DOT__ao_peripheral_subsystem_i__DOT__soc_ctrl_i__DOT__soc_ctrl_reg_top_i__DOT____Vcellout__u_boot_select__q)
            << " boot_exit_loop=" << static_cast<int>(
                root->testharness__DOT__heepsilon_top_i__DOT__x_heep_system_i__DOT__core_v_mini_mcu_i__DOT__ao_peripheral_subsystem_i__DOT__soc_ctrl_i__DOT__soc_ctrl_reg_top_i__DOT____Vcellout__u_boot_exit_loop__q)
            << " xbar_dec_valid=" << static_cast<int>(
                root->testharness__DOT__heepsilon_top_i__DOT__x_heep_system_i__DOT__core_v_mini_mcu_i__DOT__system_bus_i__DOT__system_xbar_i__DOT__gen_addr_decoders_NtoM__DOT__gen_addr_decoders__BRA__0__KET____DOT__addr_decode_i__DOT__dec_valid_o)
            << " xbar_dec_error=" << static_cast<int>(
                root->testharness__DOT__heepsilon_top_i__DOT__x_heep_system_i__DOT__core_v_mini_mcu_i__DOT__system_bus_i__DOT__system_xbar_i__DOT__gen_addr_decoders_NtoM__DOT__gen_addr_decoders__BRA__0__KET____DOT__addr_decode_i__DOT__dec_error_o)
            << " xbar_gnt=" << static_cast<int>(
                root->testharness__DOT__heepsilon_top_i__DOT__x_heep_system_i__DOT__core_v_mini_mcu_i__DOT__system_bus_i__DOT__system_xbar_i__DOT__gen_xbar_NtoM__DOT__i_xbar__DOT____Vcellout__gen_inputs__BRA__0__KET____DOT__i_addr_dec_resp_mux__gnt_o)
            << " xbar_vld=" << static_cast<int>(
                root->testharness__DOT__heepsilon_top_i__DOT__x_heep_system_i__DOT__core_v_mini_mcu_i__DOT__system_bus_i__DOT__system_xbar_i__DOT__gen_xbar_NtoM__DOT__i_xbar__DOT____Vcellout__gen_inputs__BRA__0__KET____DOT__i_addr_dec_resp_mux__vld_o)
            << " xbar_add=" << static_cast<int>(
                root->testharness__DOT__heepsilon_top_i__DOT__x_heep_system_i__DOT__core_v_mini_mcu_i__DOT__system_bus_i__DOT__system_xbar_i__DOT__gen_xbar_NtoM__DOT__i_xbar__DOT__gen_inputs__BRA__0__KET____DOT__i_addr_dec_resp_mux__DOT__add_i)
            << " xbar_bank_sel=" << static_cast<int>(
                root->testharness__DOT__heepsilon_top_i__DOT__x_heep_system_i__DOT__core_v_mini_mcu_i__DOT__system_bus_i__DOT__system_xbar_i__DOT__gen_xbar_NtoM__DOT__i_xbar__DOT__gen_inputs__BRA__0__KET____DOT__i_addr_dec_resp_mux__DOT__gen_several_outputs__DOT__bank_sel_q)
            << " xbar_inflight=" << static_cast<int>(
                root->testharness__DOT__heepsilon_top_i__DOT__x_heep_system_i__DOT__core_v_mini_mcu_i__DOT__system_bus_i__DOT__system_xbar_i__DOT__gen_xbar_NtoM__DOT__i_xbar__DOT__gen_inputs__BRA__0__KET____DOT__i_addr_dec_resp_mux__DOT__valid_inflight_q)
            << " pc_id=0x" << std::hex << std::setw(8) << std::setfill('0')
            << root->testharness__DOT__heepsilon_top_i__DOT__x_heep_system_i__DOT__core_v_mini_mcu_i__DOT__cpu_subsystem_i__DOT__gen_cv32e20__DOT__cv32e20_i__DOT__u_cve2_top__DOT__u_cve2_core__DOT__pc_id
            << " fetch_addr=0x" << std::hex << std::setw(8) << std::setfill('0')
            << root->testharness__DOT__heepsilon_top_i__DOT__x_heep_system_i__DOT__core_v_mini_mcu_i__DOT__cpu_subsystem_i__DOT__gen_cv32e20__DOT__cv32e20_i__DOT__u_cve2_top__DOT__u_cve2_core__DOT__if_stage_i__DOT__fetch_addr_n
            << " prefetch_q=0x" << std::hex << std::setw(8) << std::setfill('0')
            << root->testharness__DOT__heepsilon_top_i__DOT__x_heep_system_i__DOT__core_v_mini_mcu_i__DOT__cpu_subsystem_i__DOT__gen_cv32e20__DOT__cv32e20_i__DOT__u_cve2_top__DOT__u_cve2_core__DOT__if_stage_i__DOT__prefetch_buffer_i__DOT__fetch_addr_q
            << " boot_addr=0x" << std::hex << std::setw(8) << std::setfill('0')
            << root->testharness__DOT__heepsilon_top_i__DOT__x_heep_system_i__DOT__core_v_mini_mcu_i__DOT__ao_peripheral_subsystem_i__DOT__soc_ctrl_i__DOT__soc_ctrl_reg_top_i__DOT____Vcellout__u_boot_address__q
            << " ext_instr_resp_raw=0x" << std::hex << std::setw(9) << std::setfill('0')
            << root->testharness__DOT__heepsilon_top_i__DOT__ext_bus_i__DOT__heep_core_instr_resp_o
            << std::dec
            << " xbar_gnt_i=0x" << std::hex
            << root->testharness__DOT__heepsilon_top_i__DOT__x_heep_system_i__DOT__core_v_mini_mcu_i__DOT__system_bus_i__DOT__system_xbar_i__DOT__gen_xbar_NtoM__DOT__i_xbar__DOT__gen_inputs__BRA__0__KET____DOT__i_addr_dec_resp_mux__DOT__gnt_i
            << std::dec
            << " fifo_prod_st=" << static_cast<int>(
                root->testharness__DOT__heepsilon_top_i__DOT__x_heep_system_i__DOT__core_v_mini_mcu_i__DOT__ao_peripheral_subsystem_i__DOT__obi_fifo_i__DOT__producer_state_q)
            << " fifo_cons_st=" << static_cast<int>(
                root->testharness__DOT__heepsilon_top_i__DOT__x_heep_system_i__DOT__core_v_mini_mcu_i__DOT__ao_peripheral_subsystem_i__DOT__obi_fifo_i__DOT__consumer_state_q)
            << " fifo_req_cnt=" << static_cast<int>(
                root->testharness__DOT__heepsilon_top_i__DOT__x_heep_system_i__DOT__core_v_mini_mcu_i__DOT__ao_peripheral_subsystem_i__DOT__obi_fifo_i__DOT__obi_req_fifo_i__DOT__status_cnt_q)
            << " p2r_gnt=" << static_cast<int>(
                root->testharness__DOT__heepsilon_top_i__DOT__x_heep_system_i__DOT__core_v_mini_mcu_i__DOT__ao_peripheral_subsystem_i__DOT__periph_to_reg_i__DOT__gnt_o)
            << " p2r_rvalid=" << static_cast<int>(
                root->testharness__DOT__heepsilon_top_i__DOT__x_heep_system_i__DOT__core_v_mini_mcu_i__DOT__ao_peripheral_subsystem_i__DOT__periph_to_reg_i__DOT__r_valid_q)
            << " mux0_req_o=0x" << std::hex
            << root->testharness__DOT__heepsilon_top_i__DOT__x_heep_system_i__DOT__core_v_mini_mcu_i__DOT__system_bus_i__DOT__system_xbar_i__DOT__gen_xbar_NtoM__DOT__i_xbar__DOT____Vcellout__gen_inputs__BRA__0__KET____DOT__i_addr_dec_resp_mux__req_o
            << std::dec
            << " resp_fifo_cnt=" << static_cast<int>(
                root->testharness__DOT__heepsilon_top_i__DOT__x_heep_system_i__DOT__core_v_mini_mcu_i__DOT__ao_peripheral_subsystem_i__DOT__obi_fifo_i__DOT__obi_resp_fifo_i__DOT__status_cnt_q)
            << " prod_resp=0x" << std::hex
            << root->testharness__DOT__heepsilon_top_i__DOT__x_heep_system_i__DOT__core_v_mini_mcu_i__DOT__ao_peripheral_subsystem_i__DOT__obi_fifo_i__DOT__producer_resp_o
            << std::dec
            << " prod_st_n=" << static_cast<int>(
                root->testharness__DOT__heepsilon_top_i__DOT__x_heep_system_i__DOT__core_v_mini_mcu_i__DOT__ao_peripheral_subsystem_i__DOT__obi_fifo_i__DOT__producer_state_n)
            << std::endl;

  std::cout.flags(prev_flags);
  std::cout.fill(prev_fill);
}

void runCycles(unsigned int ncycles, Vtestharness *dut, VerilatedFstC *m_trace){
  for(unsigned int i = 0; i < 2*ncycles; i++) {
    sim_time += CLK_PERIOD_ps/2;
    dut->clk_i ^= 1;
    dut->eval();
    m_trace->dump(sim_time);
  }
}

int main (int argc, char * argv[])
{

  std::string firmware;
  vluint64_t max_sim_time;
  unsigned int boot_sel, exit_val;
  bool use_openocd;
  bool run_all = false;
  bool diag_boot = false;
  vluint64_t diag_period_cycles = 50000;

  Verilated::commandArgs(argc, argv);

  for (int i = 1; i < argc; ++i) {
    std::string arg(argv[i]);
    if (arg == "+diag_boot" || arg == "+diag_boot=1") {
      diag_boot = true;
    } else if (arg == "+diag_boot=0") {
      diag_boot = false;
    } else if (arg.rfind("+diag_period=", 0) == 0) {
      std::string value = arg.substr(std::string("+diag_period=").size());
      if (!value.empty()) {
        diag_period_cycles = std::stoull(value);
      }
    }
  }

  // Instantiate the model
  Vtestharness *dut = new Vtestharness;

  // Open VCD
  Verilated::traceEverOn (true);
  VerilatedFstC *m_trace = new VerilatedFstC;
  dut->trace (m_trace, 99);
  m_trace->open ("waveform.fst");

  XHEEP_CmdLineOptions* cmd_lines_options = new XHEEP_CmdLineOptions(argc,argv);

  use_openocd = cmd_lines_options->get_use_openocd();
  firmware = cmd_lines_options->get_firmware();

  if(firmware.empty() && use_openocd==false){
      std::cout<<"You must specify the firmware if you are not using OpenOCD"<<std::endl;
      exit(EXIT_FAILURE);
  }

  max_sim_time = cmd_lines_options->get_max_sim_time(run_all);

  boot_sel     = cmd_lines_options->get_boot_sel();

  svSetScope(svGetScopeFromName("TOP.testharness"));
  svScope scope = svGetScope();
  if (!scope) {
    std::cout<<"Warning: svGetScope failed"<< std::endl;
    exit(EXIT_FAILURE);
  }

  dut->clk_i                = 0;
  dut->rst_ni               = 1;
  dut->jtag_tck_i           = 0;
  dut->jtag_tms_i           = 0;
  // Keep JTAG TAP out of reset for non-OpenOCD testbench runs.
  dut->jtag_trst_ni         = 1;
  dut->jtag_tdi_i           = 0;
  dut->execute_from_flash_i = 0;

  dut->eval();
  m_trace->dump(sim_time);

  dut->rst_ni               = 1;
  dut->boot_select_i        = boot_sel;

  //this creates the negedge
  runCycles(20, dut, m_trace);
  dut->rst_ni               = 0;
  runCycles(40, dut, m_trace);

  dut->rst_ni = 1;
  if (diag_boot) {
    // Capture state at exact reset release (before any post-reset cycles)
    dut->eval();
    print_core_diag(dut, "reset_release_t0");
    for (int i = 0; i < 20; i++) {
      runCycles(1, dut, m_trace);
      char label[32];
      snprintf(label, sizeof(label), "reset_release_t%d", i + 1);
      print_core_diag(dut, label);
    }
    runCycles(20, dut, m_trace);
  } else {
    runCycles(40, dut, m_trace);
  }
  std::cout<<"Reset Released"<< std::endl;
  if (diag_boot) {
    print_core_diag(dut, "after_reset_release");
  }

  dut->load_flash_hex(firmware.c_str());

  if(boot_sel != 1) {
    //Booting from JTAG or loading the memory from the testbench
    if(use_openocd==false) {
      dut->tb_loadHEX(firmware.c_str());
      runCycles(1, dut, m_trace);
      //you need to exit from the bootrom loop if not using OpenOCD
      dut->tb_set_exit_loop();
      std::cout<<"Set Exit Loop"<< std::endl;
      runCycles(1, dut, m_trace);
      std::cout<<"Memory Loaded"<< std::endl;
      if (diag_boot) {
        print_core_diag(dut, "after_jtag_load");
      }
    } else {
      std::cout<<"Waiting for GDB"<< std::endl;
    }
  } else {
      std::cout<<"X-HEEP is loading from FLASH..."<< std::endl;
      if (diag_boot) {
        print_core_diag(dut, "after_flash_load");
      }
  }


  vluint64_t next_diag_cycle = diag_period_cycles;
  if(run_all==false) {
    while(dut->exit_valid_o!=1 && sim_time<max_sim_time) {
      runCycles(100, dut, m_trace);
      if (diag_boot && (sim_time / CLK_PERIOD_ps) >= next_diag_cycle) {
        print_core_diag(dut, "periodic");
        next_diag_cycle += diag_period_cycles;
      }
    }
  } else {
    while(dut->exit_valid_o!=1) {
      runCycles(100, dut, m_trace);
      if (diag_boot && (sim_time / CLK_PERIOD_ps) >= next_diag_cycle) {
        print_core_diag(dut, "periodic");
        next_diag_cycle += diag_period_cycles;
      }
    }
  }

  if (diag_boot) {
    print_core_diag(dut, "final");
  }

  std::cout<<"Simulation finished after "<<(sim_time/CLK_PERIOD_ps)<<" clock cycles"<<std::endl;

  // This should be the last message printed  so that the scripts like test-all can catch the exit value properly. 
  // The return value should be the last character (in case it is 0)
  if(dut->exit_valid_o==1) { 
    std::cout<<"Program Finished with value "<<dut->exit_value_o<<std::endl;
    exit_val = EXIT_SUCCESS;
  } else {
    std::cout<<"Simulation was terminated before program finished"<<std::endl;
    exit_val = 2; // exit 2 to indicate successful run but premature termination
  }

  m_trace->close();
  delete dut;
  delete cmd_lines_options;

  exit(exit_val);

}
