// Copyright 2022 OpenHW Group
// Copyright 2024 EPFL (Original HEEPsilon)
// Copyright 2025 University of Málaga (UMA) - Departamento de Arquitectura de Computadores
// Solderpad Hardware License, Version 2.1, see LICENSE.md for details.
// SPDX-License-Identifier: Apache-2.0 WITH SHL-2.1
//
// File: ext_xbar.sv
// Author: Cristian Campos (UMA-DAC)
// Date: 2025
//
// Description:
// External peripheral crossbar for HEEPsilon (X-HEEP v1.0.4 compatible).
//
// This is a HEEPsilon-specific adaptation of the X-HEEP testbench ext_xbar.
// The original X-HEEP version includes NAPOT (Next Address Power Of Two)
// logic to support interleaved slow memory access, which assumes index 0
// is always SLOW_MEMORY. In HEEPsilon, index 0 is the CGRA context memory,
// so the NAPOT logic must be disabled to prevent address corruption.
//
// Key differences from X-HEEP's tb/ext_xbar.sv:
// - NAPOT logic for SLOW_MEMORY interleaving is removed
// - Does not import testharness_pkg (avoids SLOW_MEMORY0_IDX dependency)
// - Simplified for single-device external slave routing (CGRA)
//
// If interleaved external memory is needed in the future, either:
// 1. Map CGRA to a different slave index (not 0)
// 2. Add configurable NAPOT logic with enable parameter

module ext_xbar #(
    parameter core_v_mini_mcu_pkg::bus_type_e BUS_TYPE = core_v_mini_mcu_pkg::BusType,
    parameter int unsigned XBAR_NMASTER = 3,
    parameter int unsigned XBAR_NSLAVE = 1,
    // Dependent parameters: do not override!
    localparam int unsigned IdxWidth = cf_math_pkg::idx_width(XBAR_NSLAVE)
) (
    input logic clk_i,
    input logic rst_ni,

    // Address map
    input addr_map_rule_pkg::addr_map_rule_t [XBAR_NSLAVE-1:0] addr_map_i,

    // Default slave index
    input logic [IdxWidth-1:0] default_idx_i,

    // Master ports
    input  obi_pkg::obi_req_t  [XBAR_NMASTER-1:0] master_req_i,
    output obi_pkg::obi_resp_t [XBAR_NMASTER-1:0] master_resp_o,

    // Slave ports
    output obi_pkg::obi_req_t  [XBAR_NSLAVE-1:0] slave_req_o,
    input  obi_pkg::obi_resp_t [XBAR_NSLAVE-1:0] slave_resp_i
);
  import obi_pkg::*;
  import core_v_mini_mcu_pkg::*;

  localparam int unsigned LOG_XBAR_NSLAVE = XBAR_NSLAVE > 1 ? $clog2(XBAR_NSLAVE) : 32'd1;

  // Aggregated Request Data (from Master -> slaves)
  // WE + BE + ADDR + WDATA
  localparam int unsigned REQ_AGG_DATA_WIDTH = 1 + 4 + 32 + 32;
  localparam int unsigned RESP_AGG_DATA_WIDTH = 32;

  // Address Decoder outputs
  /* verilator lint_off UNUSEDSIGNAL */
  logic [XBAR_NMASTER-1:0][LOG_XBAR_NSLAVE-1:0] port_sel;

  // Neck crossbar signals (only used in 1toM mode)
  obi_req_t neck_req;
  obi_resp_t neck_resp;

  // Master request/response signals (only used in NtoM mode)
  logic [XBAR_NMASTER-1:0] master_req_req;
  logic [XBAR_NMASTER-1:0] master_resp_gnt;
  logic [XBAR_NMASTER-1:0] master_resp_rvalid;
  logic [XBAR_NMASTER-1:0][31:0] master_resp_rdata;

  // Slave request/response signals (only used in NtoM mode)
  logic [XBAR_NSLAVE-1:0] slave_req_req;
  logic [XBAR_NSLAVE-1:0] slave_resp_gnt;
  logic [XBAR_NSLAVE-1:0] slave_resp_rvalid;
  logic [XBAR_NSLAVE-1:0][31:0] slave_resp_rdata;

  // Aggregated data buses (only used in NtoM mode)
  logic [XBAR_NMASTER-1:0][REQ_AGG_DATA_WIDTH-1:0] master_req_out_data;
  logic [XBAR_NSLAVE-1:0][REQ_AGG_DATA_WIDTH-1:0] slave_req_out_data;
  /* verilator lint_on UNUSEDSIGNAL */

  generate
    if (BUS_TYPE == NtoM) begin : gen_xbar_NtoM
      // ========================================
      // N-to-M Crossbar Implementation
      // ========================================

      // Address decoders for each master
      for (genvar i = 0; i < XBAR_NMASTER; i++) begin : gen_addr_decoders
        addr_decode #(
            .NoIndices(XBAR_NSLAVE),
            .NoRules(XBAR_NSLAVE),
            .addr_t(logic [31:0]),
            .rule_t(addr_map_rule_pkg::addr_map_rule_t)
        ) addr_decode_i (
            .addr_i(master_req_i[i].addr),
            .addr_map_i(addr_map_i),
            .idx_o(port_sel[i]),
            .dec_valid_o(),
            .dec_error_o(),
            .en_default_idx_i(1'b1),
            .default_idx_i(default_idx_i)
        );
      end

      // NOTE: No NAPOT (Next Address Power Of Two) logic here.
      // The X-HEEP testbench version has NAPOT for SLOW_MEMORY interleaving,
      // but HEEPsilon uses index 0 for CGRA which doesn't need address splitting.

      // Unroll OBI structs - Master side
      for (genvar i = 0; i < XBAR_NMASTER; i++) begin : gen_unroll_master
        assign master_req_req[i] = master_req_i[i].req;
        assign master_req_out_data[i] = {
          master_req_i[i].we,
          master_req_i[i].be,
          master_req_i[i].addr,
          master_req_i[i].wdata
        };
        assign master_resp_o[i].gnt = master_resp_gnt[i];
        assign master_resp_o[i].rdata = master_resp_rdata[i];
        assign master_resp_o[i].rvalid = master_resp_rvalid[i];
      end

      // Unroll OBI structs - Slave side
      for (genvar i = 0; i < XBAR_NSLAVE; i++) begin : gen_unroll_slave
        assign slave_req_o[i].req = slave_req_req[i];
        assign {
          slave_req_o[i].we,
          slave_req_o[i].be,
          slave_req_o[i].addr,
          slave_req_o[i].wdata
        } = slave_req_out_data[i];
        assign slave_resp_rdata[i] = slave_resp_i[i].rdata;
        assign slave_resp_gnt[i] = slave_resp_i[i].gnt;
        assign slave_resp_rvalid[i] = slave_resp_i[i].rvalid;
      end

      // Variable latency crossbar instantiation
      xbar_varlat #(
          .AggregateGnt(0),
          .NumIn(XBAR_NMASTER),
          .NumOut(XBAR_NSLAVE),
          .ReqDataWidth(REQ_AGG_DATA_WIDTH),
          .RespDataWidth(RESP_AGG_DATA_WIDTH)
      ) i_xbar (
          .clk_i,
          .rst_ni,
          .req_i  (master_req_req),
          .add_i  (port_sel),
          .wdata_i(master_req_out_data),
          .gnt_o  (master_resp_gnt),
          .rdata_o(master_resp_rdata),
          .rr_i   ('0),
          .vld_o  (master_resp_rvalid),
          .gnt_i  (slave_resp_gnt),
          .req_o  (slave_req_req),
          .vld_i  (slave_resp_rvalid),
          .wdata_o(slave_req_out_data),
          .rdata_i(slave_resp_rdata)
      );

    end else begin : gen_xbar_1toM
      // ========================================
      // 1-to-M Crossbar Implementation (necked)
      // ========================================

      // N-to-1 crossbar (funnel)
      xbar_varlat_n_to_one #(
          .XBAR_NMASTER(XBAR_NMASTER)
      ) i_xbar_master (
          .clk_i        (clk_i),
          .rst_ni       (rst_ni),
          .master_req_i (master_req_i),
          .master_resp_o(master_resp_o),
          .slave_req_o  (neck_req),
          .slave_resp_i (neck_resp)
      );

      // 1-to-N crossbar (fan-out)
      xbar_varlat_one_to_n #(
          .XBAR_NSLAVE   (XBAR_NSLAVE),
          .AGGREGATE_GNT (32'd0)
      ) i_xbar_slave (
          .clk_i        (clk_i),
          .rst_ni       (rst_ni),
          .addr_map_i   (addr_map_i),
          .default_idx_i(default_idx_i),
          .master_req_i (neck_req),
          .master_resp_o(neck_resp),
          .slave_req_o  (slave_req_o),
          .slave_resp_i (slave_resp_i)
      );
    end
  endgenerate

endmodule : ext_xbar
